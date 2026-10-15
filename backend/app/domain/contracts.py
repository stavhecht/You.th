"""The typed contracts every stage of the pipeline speaks.

This module is the spec. If a stage's input or output isn't described here, it
isn't defined. The two generative LLM calls use ``ExtractionOutput`` and
``SynthesisOutput`` directly as structured-output schemas, so these models are
simultaneously our internal types and the constraint the model generates under.

Field bounds (DESIGN.md §6) are enforced here as validators rather than as a
global word count, so that thin evidence naturally produces a short, honest
article instead of padding to reach a floor.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.domain.enums import SourceApi, Stance, StudyType, Verdict

#: One inline citation marker as the model emits it: ``[S1]``, or ``[S1, S5]``
#: when several sources back one statement.
#:
#: **The comma form is accepted deliberately.** The canonical multi-source
#: syntax is adjacent brackets (``[S1][S5]``), which is what ``doc_to_plain_text``
#: regenerates — but it is not a form a model produces unprompted, and the
#: synthesis prompt only ever showed a single handle. A model asked to cite
#: three sources writes ``[S1, S5, S8]``, and rejecting that cost a whole draft:
#: the body failed to parse, the article was written ``validation_failed``, and
#: it never reached the review queue.
#:
#: **This is the only definition of a marker, and tiptap.py builds on it.** The
#: parser and the handle extractor disagreeing is worse than either being
#: strict: handles inside a marker the extractor cannot read are absent from
#: ``ArticleBody.cited_handles()``, so ``was_cited`` is false for sources the article
#: visibly cites, and ``check_beats_are_cited`` reports an uncited beat that is
#: cited on the page.
CITATION_MARKER_PATTERN = r"\[S\d+(?:\s*,\s*S\d+)*\]"
CITATION_MARKER_RE = re.compile(CITATION_MARKER_PATTERN)

#: A run of adjacent markers, which the article renders as **one** citation
#: chip: ``[S1][S5]`` exactly as much as ``[S1, S5]``, because a row of separate
#: chips reads as two findings when it is one. ``tiptap.py`` splits prose on it,
#: and SYNTHESIZE counts coverage by it. Defined once, beside the marker
#: it is built from, for the same reason the marker is: if the renderer and the
#: coverage count disagreed about what one citation is, a draft could satisfy
#: the count with a row of chips the reader sees as a single citation.
#:
#: The capturing group is for ``re.split``, which keeps the runs it splits on.
CITATION_RUN_RE = re.compile(rf"((?:{CITATION_MARKER_PATTERN})+)")

#: A handle within a marker. Only ever applied to ``CITATION_MARKER_RE``
#: matches, so it does not need to guard against prose ("the S1 group").
_HANDLE_IN_MARKER_RE = re.compile(r"S\d+")

#: A citation handle in canonical form. Expressed as a schema-level pattern
#: rather than a validator on purpose: ``model_json_schema()`` carries
#: ``pattern`` into the structured-output grammar, so Ollama's sampler cannot
#: emit a malformed handle in the first place. A ``@field_validator`` is
#: invisible to the schema and only ever catches the mistake after generation.
#:
#: Written ``[0-9]`` and NOT ``\d`` deliberately. Ollama compiles this pattern
#: into a GBNF grammar and its converter does not understand the ``\d`` escape:
#: with ``\d`` the whole request fails at 400 "failed to parse grammar", which
#: breaks every synthesis call rather than just a malformed one. Verified
#: against llama3.1:8b — see tests/unitTest/test_content.py.
CitationHandle = Annotated[str, StringConstraints(pattern=r"^S[0-9]+$")]

#: The shorthand a model reaches for when every source backs the same claim:
#: "S1-S8" as one string instead of eight handles. Covers the dash variants
#: models actually emit (hyphen, en dash, em dash) and the "S1-8" short form.
_HANDLE_RANGE_RE = re.compile(r"S(\d+)\s*[-\u2013\u2014]\s*S?(\d+)")

#: Ceiling on range expansion. Retrieval keeps single digits of sources per
#: claim, so a wider span is a confused model rather than a real citation list.
#: Leaving it unexpanded lets the pattern reject it instead of inventing
#: hundreds of handles we never retrieved.
_MAX_RANGE_SPAN = 50

# A rough sentence splitter. Deliberately simple: it is used to enforce a
# *ceiling*, so over-counting on an edge case (an abbreviation like "e.g.")
# fails safe by rejecting a too-long draft rather than admitting one.
_SENTENCE_RE = re.compile(r"[.!?](?:\s|$)")


def count_sentences(text: str) -> int:
    """Sentences, counted with citation markers removed.

    A marker after the full stop ("…fell. [S5]") would otherwise be split off
    as a sentence of its own, and models place markers there often. Measured
    2026-10-01: a correct five-sentence evidence beat ending ". [S5]" counted
    as six, failed the contract twice, and the whole run failed with no
    article. Removing a marker never merges two sentences, so this still never
    under-counts.
    """
    stripped = CITATION_MARKER_RE.sub("", text)
    return len([s for s in _SENTENCE_RE.split(stripped.strip()) if s.strip()])


def extract_handles(text: str) -> set[str]:
    """Every citation handle appearing inline in a body of text.

    Two steps rather than one capturing regex, because a marker may hold
    several handles (``[S1, S5]``) and ``findall`` returns one group per match.
    """
    return {
        handle
        for marker in CITATION_MARKER_RE.findall(text)
        for handle in _HANDLE_IN_MARKER_RE.findall(marker)
    }


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# ---------------------------------------------------------------------------
# LLM call 1 — claim + ingredient extraction
# ---------------------------------------------------------------------------


class ExtractionInput(BaseModel):
    """What we know before any retrieval: a topic, and maybe marketing copy."""

    topic: NonEmptyStr = Field(description="Product or trend, e.g. 'ashwagandha for stress'")
    blurb: str | None = Field(default=None, description="Optional marketing copy")


class ExtractionOutput(BaseModel):
    """Structured-output schema for LLM call 1.

    The cap on ``target_claims`` is a cost control as much as a quality one:
    each claim fans out into its own retrieval pass across four providers.
    """

    product: NonEmptyStr
    target_claims: list[NonEmptyStr] = Field(min_length=1, max_length=6)
    ingredients: list[NonEmptyStr] = Field(default_factory=list, max_length=12)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


class CandidatePaper(BaseModel):
    """A paper as returned by a scholarly provider, normalised.

    Every provider adapter maps its own response shape into this. At least one
    of ``pmid``/``doi`` must be present — a paper we cannot resolve to a real
    identifier can never satisfy the grounding rule, so it is dropped at the
    adapter boundary rather than carried forward.
    """

    pmid: str | None = None
    doi: str | None = None
    title: NonEmptyStr
    abstract: NonEmptyStr
    journal: str | None = None
    year: int | None = None
    study_type: StudyType = StudyType.UNKNOWN
    raw_study_type: str | None = Field(
        default=None,
        description="Provider's own publication-type string, kept so the "
        "classifier can be re-run over the cache without re-fetching.",
    )
    citation_count: int | None = None
    url: str | None = None
    source_api: SourceApi

    @property
    def dedup_key(self) -> str:
        """Cross-provider identity. The first of :attr:`identity_keys`.

        DOI, then PMID, then a normalised title. The same paper routinely
        arrives from three providers with three different identifier subsets,
        so the fallback chain matters more than it looks.
        """
        return self.identity_keys[0]

    @property
    def identity_keys(self) -> list[str]:
        """*Every* key this paper could be recognised by, strongest first.

        A single key is not enough to match across providers. PubMed can return
        a record with a PMID and no DOI while OpenAlex returns the same paper
        with both — one would key on ``pmid:``, the other on ``doi:``, and
        matching on ``dedup_key`` alone would treat them as two studies. Two
        handles for one paper is the failure that makes thin evidence look
        corroborated, so the merge matches on any shared key.
        """
        keys = []
        if self.doi:
            keys.append(f"doi:{self.doi.strip().lower()}")
        if self.pmid:
            keys.append(f"pmid:{self.pmid.strip()}")
        keys.append("title:" + re.sub(r"[^a-z0-9]+", "", self.title.lower()))
        return keys


class CachedCandidate(BaseModel):
    """A candidate paired with the ``sources`` row it was written to.

    RetrieveStage produces these; RankStage consumes them. The pairing exists
    on the context because RankStage used to rebuild it by re-upserting every
    candidate a second time — 100 extra statements to recover ids the previous
    stage already held in memory, most of them single-row UPDATEs.

    A model rather than a parallel ``dedup_key -> source_id`` map, so the two
    halves cannot drift: a candidate is either carried with its id or not
    carried at all. The map version silently drops any candidate missing from
    it, and a source dropped between retrieval and ranking is invisible in the
    output — it just looks like thinner evidence.
    """

    source_id: str
    paper: CandidatePaper


class RankedSource(BaseModel):
    """A cached source with its score against one specific claim."""

    source_id: str
    claim: str
    citation_handle: str = Field(pattern=r"^S\d+$")
    paper: CandidatePaper
    cosine_similarity: float
    final_score: float = Field(
        description="cosine + grade bonus + recency bonus; see retrieval/rerank.py"
    )


# ---------------------------------------------------------------------------
# Appraisal — which way each ranked source points, per claim
# ---------------------------------------------------------------------------


class AppraisalSource(BaseModel):
    """One ranked source as the appraisal prompt renders it."""

    handle: str = Field(pattern=r"^S\d+$")
    title: str
    abstract: str
    study_type: StudyType


class AppraisalInput(BaseModel):
    """One claim and the sources RANK kept for it.

    One call per claim rather than one per article: a label is a judgement
    about a (source, claim) pair, and a paper retrieved for two claims can
    point different ways on each.
    """

    product: str
    claim: str
    sources: list[AppraisalSource] = Field(min_length=1)


class AppraisalItem(BaseModel):
    """One source's label, reasoned in the order the fields are declared.

    The order is the method, because a small model fills the grammar top to
    bottom: find the abstract's own sentence about the claim's outcome, say
    whether it really is about that outcome, then label *that sentence*.

    A verbatim ``quote`` replaced two free-text fields (what the study measured,
    and what it found), each for a measured failure on 2026-10-02. A summary of
    what a study measured named its headline topic and dropped secondary
    outcomes, so a vitamin D review reporting hip fractures was summarised as
    "bone density" and ruled off topic. And a paraphrased finding could
    disagree with the label beside it ("significantly improved muscle strength"
    labelled ``no_effect``). Searching the abstract for the outcome finds it
    wherever it is reported, and a label of a quoted sentence has one thing to
    agree with. ``appraise.label`` stores ``off_topic`` when there is no quote
    or ``reports_claim_outcome`` is false. The quote is dropped once the label
    is read.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "description": "The abstract's sentence about the claim's outcome, and its label."
        }
    )

    handle: CitationHandle = Field(description="The source's handle, e.g. S3.")
    quote: str = Field(
        description=(
            "The sentence from this abstract that reports a result for claim_outcome, "
            "copied word for word, or none."
        )
    )
    reports_claim_outcome: bool = Field(
        description=(
            "true only if quote reports a result for claim_outcome itself. false for a "
            "related outcome, and false when quote is none."
        )
    )
    stance: Stance = Field(
        description=(
            "supports: the quote reports the claimed effect. no_effect: it reports no "
            "meaningful difference. contradicts: it reports the opposite. unclear: the "
            "result is split or uncertain. off_topic: there is no quote."
        )
    )


class AppraisalOutput(BaseModel):
    """Structured-output schema for the appraisal call.

    ``claim_outcome`` is written once, before any source, so every
    ``reports_claim_outcome`` is a comparison against one stated outcome rather
    than against the model's shifting sense of what the claim meant. It names
    the *change* the claim promises, because naming only the topic was measured
    to let related outcomes through: "common cold" for "prevents the common
    cold" admitted a study of how long colds last.
    The grammar cannot force one item per handle, so ``AppraiseStage`` treats
    a missing handle as *not appraised* and drops an invented one. It can force
    *at least one*, and must: measured 2026-10-02, llama3.1:8b once wrote
    ``claim_outcome`` and stopped, returning an empty list for all twelve
    sources. ``min_length`` puts ``minItems`` in the grammar, and if a build
    ignores it, the validator sends the call through ``_chat``'s one repair
    retry instead of letting an empty answer through.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "description": "The claim's outcome, then one label for every source listed."
        }
    )

    claim_outcome: str = Field(
        description=(
            "What the claim says changes, in a few words, e.g. for 'prevents headaches': "
            "how often headaches happen."
        )
    )
    items: list[AppraisalItem] = Field(min_length=1)


class RelevanceItem(BaseModel):
    """Whether one source measured the claim's outcome — APPRAISAL_MODE=two_call.

    The two-call mode asks relevance and direction separately, so the direction
    call is never shown a study of something else. Measured 2026-10-02 it was
    level with the one-call quote design on flipped directions and a little
    behind on the rest, at twice the calls; it is kept so the comparison can be
    re-run when the model changes (see pipeline/steps/appraise.py).

    outcome_measured is written before the boolean on purpose: naming what
    the study measured is what makes a related-but-different outcome visible
    ("colds were shorter" against a claim about catching colds).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "description": "What one study measured, and whether it is the claim's outcome."
        }
    )

    handle: CitationHandle = Field(description="The source's handle, e.g. S3.")
    outcome_measured: str = Field(
        description="What this study measured, in a few words, e.g. 'mood and reaction time'."
    )
    measures_claim_outcome: bool = Field(
        description=(
            "true only if outcome_measured includes the claim's own outcome. A related "
            "outcome, a different condition or a different substance is false. A text "
            "that reports no result is false."
        )
    )


class RelevanceOutput(BaseModel):
    """Structured-output schema for the relevance call.

    ``claim_outcome`` is written once, before any source, so every
    ``measures_claim_outcome`` is a comparison against one stated outcome
    rather than against the model's shifting sense of what the claim meant.

    ``items`` has ``min_length=1``: measured, the model once wrote
    ``claim_outcome`` and stopped with an empty list. The bound puts
    ``minItems`` in the grammar, and if a build ignores it the validator sends
    the call through ``_chat``'s repair retry instead of letting it through.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "description": "The claim's outcome, then for every source whether it measured it."
        }
    )

    claim_outcome: str = Field(
        description=(
            "The outcome the claim is about, in a few words, e.g. for 'improves memory': "
            "memory."
        )
    )
    items: list[RelevanceItem] = Field(min_length=1)


class DirectionItem(BaseModel):
    """One relevant source's direction. ``finding`` comes first on purpose:
    writing down what the abstract reports before choosing a label is the
    reasoning step. Only sources the relevance call accepted are asked about.
    The finding is dropped once the label is read."""

    model_config = ConfigDict(
        json_schema_extra={"description": "What one source found for the claim."}
    )

    handle: CitationHandle = Field(description="The source's handle, e.g. S3.")
    finding: str = Field(
        description=(
            "In one short sentence, what this abstract reports for the claim's "
            "outcome, in its own words."
        )
    )
    stance: Stance = Field(
        description=(
            "supports: found the claimed effect. no_effect: tested it and found no "
            "meaningful difference. contradicts: found the opposite. unclear: no "
            "result stated for this outcome. off_topic: does not test this claim."
        )
    )


class DirectionOutput(BaseModel):
    """Structured-output schema for the direction call. ``min_length=1`` for
    the same reason as ``RelevanceOutput``."""

    model_config = ConfigDict(
        json_schema_extra={
            "description": "One label for every source listed, saying which way it points."
        }
    )

    items: list[DirectionItem] = Field(min_length=1)


# ---------------------------------------------------------------------------
# LLM call 2 — synthesis (RAG generation)
# ---------------------------------------------------------------------------


class Excerpt(BaseModel):
    """A passage from a paper's full text, shown to the model beside its abstract.

    Chosen by ``pipeline/steps/full_text.py`` for its closeness to the claims
    the paper was retrieved for. ``section`` is the heading it sits under
    ("Results"), or None for text outside any section.
    """

    section: str | None
    text: str


class PromptSource(BaseModel):
    """One source exactly as it is rendered into the synthesis prompt.

    The handle set built from these is the *only* set of ids the model may
    emit. Validation compares against this, not against the database.
    """

    handle: str = Field(pattern=r"^S\d+$")
    title: str
    abstract: str
    journal: str | None
    year: int | None
    study_type: StudyType
    source_id: str
    #: Which target claims this source was retrieved for — a list, because one
    #: paper answering two claims appears **once** in the prompt under one
    #: handle (see ``assign_handles``), and losing that would show the model
    #: the same study twice as though it were two findings.
    #:
    #: Carried purely so validation can apply the evidence quorum *per claim*.
    #: Counted article-wide, one well-supported claim lets a thin one ride
    #: along on its sources, and nothing downstream can see that happened.
    #:
    #: **Required, and non-empty.** Defaulting it to ``[]`` was tried: a source
    #: attributed to no claim supports no claim, so every draft failed the
    #: quorum check for reasons that pointed at the verdict rather than at the
    #: missing field. Fail-closed is the right direction, but a required field
    #: fails at construction instead, where the actual mistake is.
    claims: list[str] = Field(min_length=1)
    #: Full-text passages, for the few open-access papers FULL_TEXT chose.
    #: Empty for every other source — which says nothing about its quality.
    excerpts: list[Excerpt] = Field(default_factory=list)
    #: Which way APPRAISE found this source pointing, per claim it was
    #: retrieved for. Carried, like ``claims``, purely so validation can read
    #: it — ``render_source_block`` deliberately does **not** show it to the
    #: synthesis model, so the two readings stay independent and a
    #: disagreement between them is visible. A claim missing here was not
    #: appraised, which is not the same as ``unclear``.
    stances: dict[str, Stance] = Field(default_factory=dict)
    #: The claims the prompt's "Retrieved for:" line names, when that differs
    #: from ``claims``: those APPRAISE did not label ``off_topic``. ``None``
    #: means unfiltered, and the line shows ``claims``. Rendering only —
    #: validation's quorum still reads ``claims``, which is what retrieval
    #: actually did. Set only under ``synthesis_relevance_filter``.
    shown_claims: list[str] | None = None


class SynthesisInput(BaseModel):
    """What the synthesis stage was given, carried forward for validation.

    ``sources`` may be **empty**, and that is a meaningful state rather than a
    missing guard: a topic with no usable literature gets the deterministic
    no-evidence article (``services/no_evidence.py``), and validation still
    needs a payload on the context to check against — an empty handle set is
    the correct thing for it to find, because an empty one is what the article
    was written from.

    The guarantee that a *generative* call never runs on zero sources lives in
    ``SynthesizeStage``, which branches to the template before reaching the
    client. Enforcing it here instead would make the honest "no evidence"
    article unrepresentable, which is how it came to crash the pipeline.
    """

    product: str
    target_claims: list[str]
    sources: list[PromptSource] = Field(default_factory=list)

    @property
    def handle_set(self) -> set[str]:
        return {s.handle for s in self.sources}


class ArticleSection(BaseModel):
    """One titled stretch of the evidence discussion, between beats 2 and 3.

    Sections are what let an article run to a three-to-five minute read without
    becoming three unbroken blocks of prose. They are **optional and have no
    minimum** — that is the whole point. DESIGN.md §6's rule is that length is a
    ceiling and never a floor, so a `min_length` here would be a padding
    instruction: an article resting on one small trial would be obliged to
    invent two more things to say about it.

    ``body`` may hold blank-line-separated paragraphs; ``tiptap.py`` splits them
    into sibling paragraph nodes.
    """

    #: **What the model is shown, deliberately not this docstring.** Pydantic
    #: uses a class docstring as the JSON-schema ``description``, and Ollama
    #: passes that schema in as the *generation grammar* — so every word above
    #: was reaching the model at the moment it decided how much to write, and
    #: what it said was that sections are optional and that padding is
    #: forbidden. Measured: two of the first three articles carried no sections
    #: at all. The rationale is for whoever edits this file; the model needs the
    #: shape, and it gets it here.
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "One titled stretch of the evidence discussion, sitting between "
                "beats 2 and 3. The heading is a plain label of at most 8 words, "
                "never a claim. The body is at most 8 sentences and must carry at "
                "least one [S...] citation marker; separate paragraphs within it "
                "with a blank line."
            )
        }
    )


    heading: NonEmptyStr = Field(description="A plain descriptive label, not a claim")
    body: NonEmptyStr = Field(description="The section's prose, with inline citations")

    @field_validator("heading")
    @classmethod
    def _heading_at_most_8_words(cls, value: str) -> str:
        if len(value.split()) > 8:
            raise ValueError("a section heading must be at most 8 words")
        return value

    @field_validator("body")
    @classmethod
    def _body_at_most_8_sentences(cls, value: str) -> str:
        if (count := count_sentences(value)) > 8:
            raise ValueError(
                f"a section must be at most 8 sentences; this one has {count}, so "
                "merge sources that found the same thing into one sentence or "
                "move some into another section"
            )
        return value


class ArticleBody(BaseModel):
    """Three beats, plus the optional sections that sit between 2 and 3.

    Every bound here is a ceiling, not a target. A beat that would be honest at
    one sentence must stay one sentence, and an article with one usable trial
    should carry no sections at all — see DESIGN.md §6. Nothing in this system
    pads to length.

    The three beats stayed named fields when sections arrived, rather than
    becoming a list. Three things address them by name: ``derive_card`` reads
    beat 1 for the feed excerpt, ``check_beats_are_cited`` reads beat 2, and
    ``PersistStage`` places the generated lead image above beat 1. Reshaping the
    spine would have rippled into all three and bought nothing the sections do
    not already give.
    """

    #: See the note on ``ArticleSection.model_config``: the docstring above is
    #: for developers and must not be the grammar the model generates under.
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "The article body: three beats, with titled sections between "
                "beats 2 and 3 where the evidence supports them."
            )
        }
    )


    beat_1_claim: NonEmptyStr = Field(description="What it claims to do")
    beat_2_evidence: NonEmptyStr = Field(description="What the research actually shows")
    sections: list[ArticleSection] = Field(
        default_factory=list,
        max_length=5,
        # Still no `min_length`, and there must never be one — that would be the
        # padding instruction DESIGN.md §6 refuses. What changed is the word the
        # model was reading: "Optional" told it, inside its own grammar, that
        # skipping these was the easy correct answer. This states the same rule
        # the prompt already gives, and its low end is *none*, so thin evidence
        # still yields a short article.
        description=(
            "Titled sections working through the evidence in detail. How many is "
            "decided by the evidence, not by a target: six or more usable sources "
            "supports three to five sections, three to five sources supports one "
            "or two, and one or two sources supports none at all."
        ),
    )
    beat_3_bottom_line: NonEmptyStr = Field(description="Bottom line / caveat")

    @field_validator("beat_1_claim")
    @classmethod
    def _claim_at_most_four_sentences(cls, value: str) -> str:
        if (count := count_sentences(value)) > 4:
            raise ValueError(f"the claim beat must be at most 4 sentences; it has {count}")
        return value

    @field_validator("beat_2_evidence", "beat_3_bottom_line")
    @classmethod
    def _at_most_five_sentences(cls, value: str) -> str:
        if (count := count_sentences(value)) > 5:
            raise ValueError(f"this beat must be at most 5 sentences; it has {count}")
        return value

    def as_text(self) -> str:
        """Every word of the body, in reading order.

        Sections are included because ``cited_handles`` is built on this: a
        section's citations missing from the handle set would make ``was_cited``
        false for sources the article visibly cites, and would leave the
        strongest evidence in the article invisible to validation.
        """
        parts = [self.beat_1_claim, self.beat_2_evidence]
        for section in self.sections:
            parts.extend([section.heading, section.body])
        parts.append(self.beat_3_bottom_line)
        return "\n\n".join(parts)

    def cited_handles(self) -> set[str]:
        return extract_handles(self.as_text())


class CitationGroup(BaseModel):
    """One factual claim and the sources the model says support it."""

    claim: NonEmptyStr
    source_ids: list[CitationHandle] = Field(min_length=1)

    @field_validator("source_ids", mode="before")
    @classmethod
    def _normalise_handles(cls, value: Any) -> Any:
        """Expand range and comma shorthand into individual handles.

        Runs *before* the pattern check, so a model that collapses "S1", "S2",
        … "S8" into the single string "S1-S8" gets eight handles rather than a
        validation failure that fails the whole run.

        This only reshapes what the model already said; it cannot make a handle
        legitimate. Grounding is still established in ``evidence/validation.py``
        against the prompt's handle set and the database, so an expansion that
        yields a handle we never retrieved is caught there and the draft is
        discarded — the guarantee in this module's docstring is unchanged.
        """
        if not isinstance(value, list):
            return value

        expanded: list[Any] = []
        for entry in value:
            if not isinstance(entry, str):
                expanded.append(entry)
                continue
            for part in (piece.strip() for piece in entry.split(",")):
                if not part:
                    continue
                match = _HANDLE_RANGE_RE.fullmatch(part)
                if match is not None:
                    start, end = int(match.group(1)), int(match.group(2))
                    if 0 <= end - start < _MAX_RANGE_SPAN:
                        expanded.extend(f"S{n}" for n in range(start, end + 1))
                        continue
                expanded.append(part)

        # Order-preserving dedupe: "S1-S3" alongside a stray "S2" is one source
        # list, not a repeated citation. These lists are single digits long.
        deduped: list[Any] = []
        for handle in expanded:
            if handle not in deduped:
                deduped.append(handle)
        return deduped


class SynthesisOutput(BaseModel):
    """Structured-output schema for LLM call 2.

    Schema-level constraints catch shape errors. They do NOT establish
    grounding — that is ``evidence/validation.py``'s job, and it runs against
    the prompt's handle set and the database, neither of which the model can
    influence.
    """

    #: See ``ArticleSection.model_config``. Same split, same reason: the
    #: docstring names the module that enforces grounding, which is what a
    #: maintainer needs and is noise inside the model's generation grammar.
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "One finished article: a headline, a verdict on whether the "
                "sources show the claims as written are true, a "
                "two-sentence summary, the body, and the list of sources cited."
            )
        }
    )


    headline: NonEmptyStr
    verdict: Verdict
    verdict_qualifier: str | None = Field(
        default=None, description="Optional single clause, e.g. 'for sleep quality only'"
    )
    summary: NonEmptyStr
    body: ArticleBody
    citations: list[CitationGroup] = Field(default_factory=list)

    @field_validator("headline")
    @classmethod
    def _headline_at_most_12_words(cls, value: str) -> str:
        if len(value.split()) > 12:
            raise ValueError("headline must be at most 12 words")
        return value

    @field_validator("summary")
    @classmethod
    def _summary_at_most_2_sentences(cls, value: str) -> str:
        if (count := count_sentences(value)) > 2:
            raise ValueError(f"summary must be at most 2 sentences; it has {count}")
        return value

    @field_validator("verdict_qualifier")
    @classmethod
    def _qualifier_is_one_clause(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value.split()) > 15 or count_sentences(value) > 1:
            raise ValueError("verdict qualifier must be a single short clause")
        return value

    def all_cited_handles(self) -> set[str]:
        """Every handle the model emitted, inline or in the citations list."""
        handles = self.body.cited_handles()
        for group in self.citations:
            handles.update(group.source_ids)
        return handles


# ---------------------------------------------------------------------------
# Reader edit — the plain-language pass over a finished draft
# ---------------------------------------------------------------------------


class ReaderEditInput(BaseModel):
    """One paragraph of a finished draft, sent to be reworded for readers.

    ``earlier`` is the article so far, already edited, so a term explained
    there is not explained again. ``max_sentences`` is the limit of the part
    the paragraph belongs to (beat or section), which the edit must keep.
    ``feedback`` is set on the one retry a paragraph gets: why the last edit
    was thrown away, or what a kept edit still left to fix.
    """

    product: str
    earlier: list[str]
    paragraph: str
    max_sentences: int
    feedback: str | None = None


class ReaderEditOutput(BaseModel):
    """Structured-output schema for the reader edit: the paragraph's sentences,
    reworded, in order.

    Sentences rather than one string because the paragraph goes in numbered:
    asked for a paragraph, Ternary-Bonsai reworded its first sentence and copied
    the rest (2026-10-15). Whether the rewording changed anything but the
    wording is decided by ``pipeline/steps/reader_edit.py``, never by the model.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Every sentence of the paragraph, edited for everyday readers, in order."
            )
        }
    )

    sentences: list[NonEmptyStr] = Field(min_length=1)

    @property
    def text(self) -> str:
        return " ".join(sentence.strip() for sentence in self.sentences)


# ---------------------------------------------------------------------------
# Illustration — generated imagery
# ---------------------------------------------------------------------------


class GeneratedImage(BaseModel):
    """One render, already written to the media store.

    ``src`` is the path a document may hold — it comes back from
    ``services/media.py::store_image``, so it satisfies ``MEDIA_SRC_RE`` by
    construction rather than by a later check.

    **Provenance is per frame, and that is a deliberate move down from
    ``Illustration``.** ``prompt``, ``negative_prompt``, ``model`` and ``seed``
    were shared fields on the pair for as long as the only way to draw either
    frame was to draw both. A reviewer can now redraw one on its own, and a
    single stored prompt would then be a true record of one picture and a false
    record of the other — the kind of quietly wrong provenance that is worse
    than none, because it still answers when asked.

    They are stored rather than recomputed because ``imagery/prompt.py`` is
    going to change and "which words produced this picture" stops being
    answerable the moment it does. Provenance outranks tidiness here, the same
    argument as the retracted-source rows.

    The three string fields default to empty so rows written before the move
    still validate; ``Illustration`` pushes their old shared values down into
    the frames on the way in, so nothing is actually lost.
    """

    src: str
    alt: str
    width: int
    height: int
    seed: int
    prompt: str = ""
    negative_prompt: str = ""
    #: Provider-namespaced, e.g. ``huggingface/black-forest-labs/FLUX.1-schnell``.
    model: str = ""


class Illustration(BaseModel):
    """Both pictures an article has: the one in its body, and the tile's.

    **They are not one photograph at two aspect ratios**, and they are no
    longer even guaranteed to come from one generation — a reviewer can redraw
    either alone. They are two renders of the same subject under the same
    locked, claim-free treatment; see ``services/illustration.py`` for the
    measurement that killed the stronger claim.

    What survives all of that is the property worth having, and
    ``services/card.py`` is what enforces it: the cover reaches the feed only
    while ``lead.src`` is still the document's first image, so the tile can
    never show a picture belonging to an article this one has stopped being.
    Neither frame asserts anything, so neither can assert what the other does
    not — which is why redrawing one and not the other is safe.
    """

    lead: GeneratedImage
    cover: GeneratedImage

    @model_validator(mode="before")
    @classmethod
    def _carry_legacy_shared_fields(cls, value: Any) -> Any:
        """Push a pre-split row's shared ``prompt``/``model`` onto both frames.

        Rows written while the pair shared one prompt keep it at the top level,
        where nothing reads it any more. Dropping it would silently discard the
        only record of how those two pictures were made, on the first partial
        redraw — so it is moved down instead. A frame that carries its own
        value always wins, which is what makes this safe to run on new rows too.
        """
        if not isinstance(value, dict):
            return value
        shared = {
            key: value[key]
            for key in ("prompt", "negative_prompt", "model")
            if isinstance(value.get(key), str)
        }
        if not shared:
            return value

        patched = dict(value)
        for frame in ("lead", "cover"):
            existing = patched.get(frame)
            if isinstance(existing, dict):
                patched[frame] = {**shared, **existing}
        return patched


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationFailure(BaseModel):
    code: str = Field(
        description="hallucinated_handle | unresolvable_source | uncited_beat "
        "| uncited_section | verdict_exceeds_grade | malformed_body | unsourced_number "
        "| no_evidence_with_citations"
    )
    message: str
    detail: dict[str, object] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    """Stored on the article and surfaced in the review UI.

    ``citations_resolved``/``citations_total`` is the '4/4 citations resolve'
    badge — computed once at draft time, not recomputed in the browser.
    """

    passed: bool
    citations_total: int
    citations_resolved: int
    best_evidence_grade: StudyType
    #: The strongest verdict the cited evidence would have allowed.
    #:
    #: Recorded even when the draft passes, which is the whole point. The cap
    #: is a ceiling and nothing raises a verdict toward it, so an over-confident
    #: draft fails loudly while an under-confident one is indistinguishable from
    #: a correct cautious call — and the first three articles were all "weak"
    #: against ceilings that permitted "supported", with a clean report each
    #: time. Storing the ceiling beside the verdict is what lets a reviewer see
    #: the gap; without it this class of failure has no signal anywhere.
    #:
    #: Optional because reports written before this field existed do not carry
    #: it, and a missing ceiling must read as "not recorded" rather than as
    #: ``no_evidence``, which is a real verdict and the falsest thing a default
    #: could say here.
    verdict_ceiling: Verdict | None = None
    #: Per claim, how the cited trials and reviews were appraised: ``for``,
    #: ``against`` (no effect or the opposite) and ``unlabelled``
    #: (``grading.strong_by_direction``). None when nothing was appraised —
    #: the stage was off or failed, or the report predates it — which must not
    #: read as "no conflict". Recorded so the effect of letting these labels
    #: gate a verdict can be measured on real articles before anyone does it.
    stance_tally: dict[str, dict[str, int]] | None = None
    failures: list[ValidationFailure] = Field(default_factory=list)
    #: Problems a reviewer should look at that do not block the draft. A check
    #: that is usually a mistake but sometimes honest — a ``no_evidence``
    #: verdict citing studies that found *no effect* — fails the first draft so
    #: the model is asked once, and lands here if the rewrite keeps it.
    warnings: list[ValidationFailure] = Field(default_factory=list)

    @property
    def badge(self) -> str:
        return f"{self.citations_resolved}/{self.citations_total} citations resolve"
