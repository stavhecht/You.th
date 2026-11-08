"""What one evaluation case says, and what it expects.

**The pipeline takes a topic, not a conversation.** ``PipelineContext`` carries
``topic`` and an optional ``blurb`` and nothing else, so a case's ``query`` is
handed to EXTRACT verbatim as the topic — exactly what a reviewer typing a
question into the desk would produce. A multi-turn case runs only its *last*
turn, because earlier turns cannot reach the pipeline at all; what such a case
measures is whether the system fails safely without the context it never had.

**Every expectation is optional.** A case with nothing but a query still yields
the deterministic metrics that need no ground truth (termination, citation
validity, groundedness, latency). Ground truth only ever *adds* checks, so a
dataset can grow to hundreds of cases without hand-writing an answer for each.

**Expectations are written against this system's real tools.** ``expected_tools``
names what the pipeline actually calls (``pubmed``, ``europe_pmc``, ``openalex``,
``llm_extraction``…). Where a better-equipped system would reach for something
this one does not have — a news API for "this week's headlines" — that goes in
``ideal_tools``, which feeds the capability-gap section of the report and never
the accuracy metric. Scoring a system on a tool it was never given measures the
dataset, not the system.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.domain.enums import Stance, Verdict


class Category(StrEnum):
    SIMPLE_FACTUAL = "simple_factual"
    MULTI_PART = "multi_part"
    LATEST_RESEARCH = "latest_research"
    ACADEMIC_RESEARCH = "academic_research"
    GENERAL_WEB = "general_web"
    AMBIGUOUS = "ambiguous"
    FOLLOW_UP = "follow_up"
    NO_ANSWER = "no_answer"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    ADVERSARIAL = "adversarial"
    FAILURE = "failure"


class Behavior(StrEnum):
    """What the case author considers correct, in terms the trace can confirm."""

    ANSWER_WITH_CITATIONS = "answer_with_citations"
    ACKNOWLEDGE_INSUFFICIENT_EVIDENCE = "acknowledge_insufficient_evidence"
    ACKNOWLEDGE_UNCERTAINTY = "acknowledge_uncertainty"
    REJECT_FALSE_PREMISE = "reject_false_premise"
    REFUSE_OR_CLARIFY = "refuse_or_clarify"
    CONTROLLED_FAILURE = "controlled_failure"
    DEGRADE_GRACEFULLY = "degrade_gracefully"


class Outcome(StrEnum):
    """What actually happened to a run, read off the trace.

    The pipeline cannot ask a clarifying question and has no "I don't know"
    reply; its honest refusals are the deterministic no-evidence article and
    the ``UnanchoredQuery`` stage failure. Those are the outcomes a refusal can
    take here.
    """

    #: Validated article (would be ``pending_review``), verdict above no_evidence.
    ANSWERED = "answered"
    #: Validated article whose verdict is ``no_evidence``.
    NO_EVIDENCE = "no_evidence"
    #: Article that failed validation (would be stored ``validation_failed`` and
    #: never reach the review queue).
    ANSWERED_UNVALIDATED = "answered_unvalidated"
    #: RETRIEVE refused a query naming no substance (non-retryable).
    REFUSED_UNANCHORED = "refused_unanchored"
    #: A ``StageError`` marked retryable: the worker would requeue the run.
    FAILED_RETRYABLE = "failed_retryable"
    #: A ``StageError`` marked permanent, other than the unanchored refusal.
    FAILED_PERMANENT = "failed_permanent"
    #: A stage raised something that was not a ``StageError``. The orchestrator
    #: still catches it and fails the run with a traceback, so the worker
    #: survives — but nothing classified it, so a transient cause is never
    #: retried.
    FAILED_UNEXPECTED = "failed_unexpected"
    #: The graph hit its recursion limit: a loop the bounds did not contain.
    LOOP_LIMIT = "loop_limit"
    #: The case exceeded ``case_timeout_seconds``.
    TIMEOUT = "timeout"
    #: Something escaped the graph entirely — a harness bug, not a system result.
    CRASHED = "crashed"
    #: The suite stopped after RANK on purpose (retrieval-only evaluation).
    RETRIEVAL_ONLY = "retrieval_only"
    #: Not executed; ``skip_reason`` says why.
    SKIPPED = "skipped"
    #: A research-agent scenario that ran to completion.
    RESEARCH_COMPLETED = "research_completed"
    #: A research-agent scenario that ended with ``state.failed`` set.
    RESEARCH_FAILED = "research_failed"


#: Outcomes in which the system stopped on its own terms. The complement is
#: what ``successful_termination`` counts as a failure.
TERMINATED = frozenset(Outcome) - {
    Outcome.LOOP_LIMIT,
    Outcome.TIMEOUT,
    Outcome.CRASHED,
    Outcome.SKIPPED,
}

#: Outcomes that produced an article a reader could eventually see.
ARTICLE_OUTCOMES = frozenset({Outcome.ANSWERED, Outcome.NO_EVIDENCE})

DEFAULT_OUTCOMES: dict[Behavior, frozenset[Outcome]] = {
    Behavior.ANSWER_WITH_CITATIONS: frozenset({Outcome.ANSWERED}),
    Behavior.ACKNOWLEDGE_INSUFFICIENT_EVIDENCE: frozenset(
        {Outcome.NO_EVIDENCE, Outcome.ANSWERED}
    ),
    Behavior.ACKNOWLEDGE_UNCERTAINTY: frozenset({Outcome.ANSWERED}),
    Behavior.REJECT_FALSE_PREMISE: frozenset({Outcome.ANSWERED, Outcome.NO_EVIDENCE}),
    Behavior.REFUSE_OR_CLARIFY: frozenset({Outcome.REFUSED_UNANCHORED, Outcome.NO_EVIDENCE}),
    Behavior.CONTROLLED_FAILURE: frozenset(
        {
            Outcome.FAILED_RETRYABLE,
            Outcome.FAILED_PERMANENT,
            Outcome.REFUSED_UNANCHORED,
            Outcome.ANSWERED_UNVALIDATED,
            Outcome.RESEARCH_FAILED,
        }
    ),
    # Degrading gracefully is finishing the run on what still works. Whether the
    # draft then passes validation is a quality question with its own metrics:
    # a rejected draft is the quality gate doing its job, not a failed recovery.
    Behavior.DEGRADE_GRACEFULLY: frozenset(
        {
            Outcome.ANSWERED,
            Outcome.NO_EVIDENCE,
            Outcome.ANSWERED_UNVALIDATED,
            Outcome.RESEARCH_COMPLETED,
        }
    ),
}

#: Verdicts acceptable by default, when a case states none. ``None`` means the
#: behaviour places no constraint on the verdict.
DEFAULT_VERDICTS: dict[Behavior, frozenset[Verdict] | None] = {
    Behavior.ANSWER_WITH_CITATIONS: None,
    Behavior.ACKNOWLEDGE_INSUFFICIENT_EVIDENCE: frozenset({Verdict.NO_EVIDENCE, Verdict.WEAK}),
    Behavior.ACKNOWLEDGE_UNCERTAINTY: frozenset({Verdict.MIXED, Verdict.WEAK}),
    Behavior.REJECT_FALSE_PREMISE: frozenset({Verdict.NO_EVIDENCE, Verdict.WEAK}),
    Behavior.REFUSE_OR_CLARIFY: None,
    Behavior.CONTROLLED_FAILURE: None,
    Behavior.DEGRADE_GRACEFULLY: None,
}


class Turn(BaseModel):
    query: str


class FaultSpec(BaseModel):
    """One injected failure.

    ``target`` is a tool name as the trace records it (``pubmed``,
    ``europe_pmc``, ``openalex``, ``europe_pmc_lookup``, ``europe_pmc_fulltext``),
    ``all_search`` for every search provider at once, or a component
    (``llm_extraction``, ``llm_appraisal``, ``llm_synthesis``, ``embeddings``,
    ``vector_store``).
    """

    target: str
    kind: Literal[
        "status",
        "timeout",
        "connect_error",
        "rate_limit",
        "malformed_json",
        "invalid_xml",
        "empty",
        "body_throttle",
        "error",
        "hallucinate",
    ]
    status: int | None = None
    #: Restrict to one operation of the tool (``esearch``, ``efetch``, ``search``).
    operation: str | None = None
    #: Only calls whose parameters contain this text (e.g. ``PUB_TYPE`` to fault
    #: Europe PMC's reviews search but not its general one).
    param_contains: str | None = None
    #: Let this many matching calls through before the fault starts.
    after: int = 0
    #: Fire at most this many times; ``None`` is every matching call.
    times: int | None = None


class InjectedDocument(BaseModel):
    """A synthetic record appended to one provider's results.

    Used for prompt-injection cases: the abstract carries instructions, and the
    case checks the article did not follow them. Its DOI must use the reserved
    ``10.0000/eval-`` prefix so the trace and the source validator can tell it
    from a real paper and keep it out of the existence metric.
    """

    provider: str = "pubmed"
    title: str
    abstract: str
    doi: str
    publication_types: list[str] = Field(default_factory=list)
    year: int | None = None
    journal: str | None = None

    @model_validator(mode="after")
    def _reserved_prefix(self) -> InjectedDocument:
        if not self.doi.startswith("10.0000/eval-"):
            raise ValueError("an injected document's DOI must start with 10.0000/eval-")
        return self


class EvalCase(BaseModel):
    """One test case. Unknown fields are refused, so a typo fails loudly."""

    model_config = {"extra": "forbid"}

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    category: Category
    subcategory: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)

    query: str | None = None
    turns: list[Turn] = Field(default_factory=list)
    blurb: str | None = None

    # --- expected behaviour ---------------------------------------------------
    expected_behavior: Behavior = Behavior.ANSWER_WITH_CITATIONS
    acceptable_outcomes: list[Outcome] | None = None
    acceptable_verdicts: list[Verdict] | None = None

    # --- query understanding --------------------------------------------------
    #: Any of these must appear in the extracted product or ingredients. Also
    #: the subject half of the document-relevance rule.
    expected_subject: list[str] = Field(default_factory=list)
    #: Any of these should appear in the extracted claims. Also the outcome half
    #: of the document-relevance rule.
    expected_outcome_terms: list[str] = Field(default_factory=list)
    #: For a follow-up: the subject of the conversation, which the last turn
    #: may not name.
    conversation_subject: list[str] = Field(default_factory=list)

    # --- retrieval ------------------------------------------------------------
    #: Known relevant papers, ``pmid:<id>`` or ``doi:<doi>``. Every one is
    #: verified against PubMed before it is used as ground truth.
    relevant_ids: list[str] = Field(default_factory=list)
    requires_recency: bool = False

    # --- appraisal --------------------------------------------------------------
    #: Hand-labelled direction of known papers, **per claim**: claim text, as
    #: extraction worded it in the recorded run, to ``pmid:<id>`` / ``doi:<doi>``
    #: to stance. Per claim because a label is about a (paper, claim) pair — the
    #: same coffee trial ``supports`` "has no effect on heart health" and
    #: ``no_effect`` for "is bad for heart health". Labelled from the abstract
    #: against the claim as written, never from what is known about the
    #: substance elsewhere: APPRAISE is only shown the abstract. Scored only
    #: where APPRAISE labelled that paper for that claim; a claim worded
    #: differently in a new run is reported as unmatched, not scored.
    expected_stances: dict[str, dict[str, Stance]] = Field(default_factory=dict)

    # --- tools ----------------------------------------------------------------
    expected_tools: list[str] | None = None
    forbidden_tools: list[str] = Field(default_factory=list)
    ideal_tools: list[str] = Field(default_factory=list)

    # --- answer ---------------------------------------------------------------
    expected_source_types: list[str] = Field(default_factory=list)
    #: Groups of synonyms; each group must be represented in the article.
    required_concepts: list[list[str]] = Field(default_factory=list)
    expected_claims: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    #: Literal strings that must not appear (case-insensitive).
    forbidden_phrases: list[str] = Field(default_factory=list)
    min_citations: int | None = None

    # --- failure testing ------------------------------------------------------
    faults: list[FaultSpec] = Field(default_factory=list)
    inject_documents: list[InjectedDocument] = Field(default_factory=list)
    #: Tools that must still return results despite the faults.
    expected_fallback: list[str] = Field(default_factory=list)
    #: Whether the run's failure should be marked retryable (transient cause).
    expect_retryable: bool | None = None
    #: ``scripted`` swaps the synthesis model for a deterministic writer, for
    #: cases whose subject is control flow rather than prose.
    llm: Literal["real", "scripted"] = "real"
    #: A research-agent scenario name instead of a pipeline query.
    research_scenario: str | None = None

    @model_validator(mode="after")
    def _has_an_input(self) -> EvalCase:
        if self.research_scenario is None and not (self.query or self.turns):
            raise ValueError(f"{self.id}: a case needs a query, turns or a research_scenario")
        if self.query and self.turns:
            raise ValueError(f"{self.id}: give either query or turns, not both")
        malformed = [
            key
            for labels in self.expected_stances.values()
            for key in labels
            if not key.startswith(("pmid:", "doi:"))
        ]
        if malformed:
            raise ValueError(
                f"{self.id}: expected_stances keys must be pmid:<id> or doi:<doi>, "
                f"got {malformed}"
            )
        return self

    @property
    def executed_query(self) -> str:
        """What reaches the pipeline: the query, or the last turn."""
        if self.turns:
            return self.turns[-1].query
        return self.query or ""

    @property
    def conversation(self) -> str:
        """Every turn, for the judge — it should see what the user meant."""
        if not self.turns:
            return self.query or ""
        return "\n".join(
            f"User{' (follow-up)' if index else ''}: {turn.query}"
            for index, turn in enumerate(self.turns)
        )

    def outcomes(self) -> frozenset[Outcome]:
        if self.acceptable_outcomes is not None:
            return frozenset(self.acceptable_outcomes)
        return DEFAULT_OUTCOMES[self.expected_behavior]

    def verdicts(self) -> frozenset[Verdict] | None:
        if self.acceptable_verdicts is not None:
            return frozenset(self.acceptable_verdicts)
        return DEFAULT_VERDICTS[self.expected_behavior]

    @property
    def is_failure_case(self) -> bool:
        return bool(self.faults or self.research_scenario) or self.category is Category.FAILURE
