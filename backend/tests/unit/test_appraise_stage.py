"""APPRAISE: which way each ranked source points, and why nothing here can fail
a run or change a verdict yet.

Phase 1 of claim refutation. The labels are recorded per (source, claim), shown
to reviewers, and checked against the verdict as a *warning*. They are hidden
from the synthesis model on purpose, so the two readings of the abstracts stay
independent.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.domain.contracts import (
    AppraisalInput,
    AppraisalItem,
    AppraisalOutput,
    AppraisalSource,
    CandidatePaper,
    CitationGroup,
    DirectionItem,
    DirectionOutput,
    ExtractionOutput,
    PromptSource,
    RankedSource,
    RelevanceItem,
    RelevanceOutput,
    SynthesisInput,
)
from app.domain.enums import SourceApi, Stance, StudyType, Verdict
from app.evidence.grading import strong_by_direction
from app.evidence.validation import validate_draft
from app.llm.base import LLMError, LLMResult, TokenUsage
from app.llm.prompts.appraisal import (
    build_appraisal_user_prompt,
    build_direction_user_prompt,
    build_relevance_user_prompt,
)
from app.llm.prompts.synthesis import render_source_block
from app.pipeline.stages import PipelineContext, StageName
from app.pipeline.steps.appraise import AppraiseStage, label
from app.pipeline.steps.synthesize import SynthesizeStage
from evaluation.harness.llm import synthesis_key
from evaluation.metrics import stance as stance_metrics
from tests.conftest import FakeSession, FakeSource, make_draft

CLAIM = "prevents the common cold"
OTHER = "shortens colds"
MODEL = "ollama/llama3.1:8b"


def _ranked(source_id: str, claim: str, handle: str) -> RankedSource:
    return RankedSource(
        source_id=source_id,
        claim=claim,
        citation_handle=handle,
        paper=CandidatePaper(
            pmid=f"9{source_id}",
            title=f"Trial {source_id} [a translated title]",
            abstract=f"Abstract of {source_id}. Risk ratio 0.97 (95% CI [0.94, 1.00]).",
            study_type=StudyType.RCT,
            source_api=SourceApi.PUBMED,
        ),
        cosine_similarity=0.8,
        final_score=0.9,
    )


def _ctx(**claims: list[str]) -> PipelineContext:
    """Claim -> source ids, one handle per source as ``assign_handles`` gives."""
    handles: dict[str, str] = {}
    ranked: dict[str, list[RankedSource]] = {}
    for key, ids in claims.items():
        claim = key.replace("_", " ")
        ranked[claim] = [
            _ranked(sid, claim, handles.setdefault(sid, f"S{len(handles) + 1}")) for sid in ids
        ]
    return PipelineContext(
        run_id="run-1",
        topic="vitamin c",
        extraction=ExtractionOutput(
            product="Vitamin C", target_claims=list(ranked), ingredients=["vitamin c"]
        ),
        ranked=ranked,
    )


def _item(
    handle: str, stance: Stance, *, measured: bool | None = None, quote: str | None = None
) -> AppraisalItem:
    """An item whose relevance agrees with its stance unless told otherwise."""
    relevant = stance is not Stance.OFF_TOPIC if measured is None else measured
    return AppraisalItem(
        handle=handle,
        quote=quote if quote is not None else ("Colds were fewer." if relevant else "none"),
        reports_claim_outcome=relevant,
        stance=stance,
    )


class _Client:
    """Answers each claim from a script: handle -> stance, or an exception."""

    def __init__(self, script: dict[str, dict[str, Stance] | Exception]) -> None:
        self._script = script
        self.calls: list[AppraisalInput] = []

    async def appraise(self, payload: AppraisalInput) -> LLMResult[AppraisalOutput]:
        self.calls.append(payload)
        answer = self._script[payload.claim]
        if isinstance(answer, Exception):
            raise answer
        return LLMResult(
            output=AppraisalOutput(
                claim_outcome="catching a cold",
                items=[_item(handle, stance) for handle, stance in answer.items()],
            ),
            usage=TokenUsage(input_tokens=100, output_tokens=20),
            model="ollama/qwen2.5:7b-instruct",
        )


# --- the stage -------------------------------------------------------------------


async def test_each_pair_gets_its_label_keyed_by_claim_and_source() -> None:
    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    client = _Client({CLAIM: {"S1": Stance.NO_EFFECT, "S2": Stance.SUPPORTS}})

    result = await AppraiseStage(client).run(ctx)

    assert result.stances == {CLAIM: {"a": Stance.NO_EFFECT, "b": Stance.SUPPORTS}}
    metrics = result.metrics[StageName.APPRAISE]
    assert metrics["cause"] is None
    assert (metrics["offered"], metrics["labelled"], metrics["missing"]) == (2, 2, 0)
    assert metrics["by_stance"] == {"no_effect": 1, "supports": 1}
    assert result.usage_by_stage[StageName.APPRAISE].usage.input_tokens == 100


async def test_a_direction_given_to_a_study_of_something_else_is_overruled() -> None:
    """The measured failure: "colds were shorter" read as support for
    "prevents colds". The model says the study did not measure the claim's
    outcome, and that answer wins over the direction it gave anyway."""

    class _Inconsistent(_Client):
        async def appraise(self, payload: AppraisalInput) -> LLMResult[AppraisalOutput]:
            return LLMResult(
                output=AppraisalOutput(
                    claim_outcome="catching a cold",
                    items=[
                        _item("S1", Stance.SUPPORTS, measured=False),
                        _item("S2", Stance.NO_EFFECT),
                    ],
                ),
                usage=TokenUsage(),
            )

    result = await AppraiseStage(_Inconsistent({})).run(
        _ctx(prevents_the_common_cold=["a", "b"])
    )

    assert result.stances == {CLAIM: {"a": Stance.OFF_TOPIC, "b": Stance.NO_EFFECT}}
    assert result.metrics[StageName.APPRAISE]["relevance_overrides"] == 1


def test_relevance_outranks_direction() -> None:
    assert label(_item("S1", Stance.SUPPORTS, measured=False)) is Stance.OFF_TOPIC
    assert label(_item("S1", Stance.CONTRADICTS, measured=True)) is Stance.CONTRADICTS
    # Off topic stays off topic even if the model contradicts itself the other way.
    assert label(_item("S1", Stance.OFF_TOPIC, measured=True)) is Stance.OFF_TOPIC
    # No quote means nothing was reported, whatever the boolean beside it says.
    for empty in ("none", "None.", "", "  "):
        item = _item("S1", Stance.SUPPORTS, measured=True, quote=empty)
        assert label(item) is Stance.OFF_TOPIC


async def test_one_call_per_claim_with_that_claims_sources_only() -> None:
    """A label is about a (source, claim) pair: a paper retrieved for two claims
    can point different ways on each."""
    ctx = _ctx(prevents_the_common_cold=["a", "b"], shortens_colds=["b"])
    client = _Client(
        {
            CLAIM: {"S1": Stance.NO_EFFECT, "S2": Stance.NO_EFFECT},
            OTHER: {"S2": Stance.SUPPORTS},
        }
    )

    result = await AppraiseStage(client).run(ctx)

    assert [[s.handle for s in call.sources] for call in client.calls] == [["S1", "S2"], ["S2"]]
    assert result.stances[CLAIM]["b"] is Stance.NO_EFFECT
    assert result.stances[OTHER]["b"] is Stance.SUPPORTS


async def test_a_skipped_handle_is_not_appraised_rather_than_unclear() -> None:
    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    client = _Client({CLAIM: {"S1": Stance.NO_EFFECT}})

    result = await AppraiseStage(client).run(ctx)

    assert result.stances == {CLAIM: {"a": Stance.NO_EFFECT}}
    assert result.metrics[StageName.APPRAISE]["missing"] == 1


async def test_an_invented_handle_is_dropped_never_guessed_onto_a_source() -> None:
    ctx = _ctx(prevents_the_common_cold=["a"])
    client = _Client({CLAIM: {"S1": Stance.SUPPORTS, "S9": Stance.NO_EFFECT}})

    result = await AppraiseStage(client).run(ctx)

    assert result.stances == {CLAIM: {"a": Stance.SUPPORTS}}
    assert result.metrics[StageName.APPRAISE]["invented"] == 1


async def test_a_failed_call_costs_only_its_claim_and_its_tokens_are_recorded() -> None:
    """The stage never fails a run, and a failed call that the provider answered
    is billed like a success."""
    failure = LLMError(
        "appraisal: output still failed validation",
        usage=TokenUsage(input_tokens=50, output_tokens=10),
        model="ollama/qwen2.5:7b-instruct",
    )
    ctx = _ctx(prevents_the_common_cold=["a"], shortens_colds=["b"])
    client = _Client({CLAIM: failure, OTHER: {"S2": Stance.SUPPORTS}})

    result = await AppraiseStage(client).run(ctx)

    assert result.stances == {OTHER: {"b": Stance.SUPPORTS}}
    metrics = result.metrics[StageName.APPRAISE]
    assert (metrics["cause"], metrics["failed_calls"]) == ("partial_model_error", 1)
    assert result.usage_by_stage[StageName.APPRAISE].usage.input_tokens == 150


async def test_every_call_failing_is_recorded_not_raised() -> None:
    ctx = _ctx(prevents_the_common_cold=["a"])
    client = _Client({CLAIM: LLMError("is `ollama serve` running?", retryable=True)})

    result = await AppraiseStage(client).run(ctx)

    assert result.stances == {}
    assert result.metrics[StageName.APPRAISE]["cause"] == "model_error"


async def test_an_unexpected_error_keeps_the_claims_already_labelled() -> None:
    ctx = _ctx(prevents_the_common_cold=["a"], shortens_colds=["b"])
    client = _Client({CLAIM: {"S1": Stance.NO_EFFECT}, OTHER: RuntimeError("a bug")})

    result = await AppraiseStage(client).run(ctx)

    assert result.stances == {CLAIM: {"a": Stance.NO_EFFECT}}
    assert result.metrics[StageName.APPRAISE]["cause"] == "unexpected"


async def test_switched_off_it_makes_no_call() -> None:
    result = await AppraiseStage(None).run(_ctx(prevents_the_common_cold=["a"]))

    assert result.stances == {}
    assert result.metrics[StageName.APPRAISE]["cause"] == "disabled"


async def test_nothing_ranked_means_nothing_to_label() -> None:
    client = _Client({})
    ctx = _ctx(prevents_the_common_cold=["a"]).model_copy(update={"ranked": {CLAIM: []}})

    result = await AppraiseStage(client).run(ctx)

    assert client.calls == []
    assert result.metrics[StageName.APPRAISE]["cause"] == "no_sources"


def test_appraise_runs_after_full_text_and_before_synthesis() -> None:
    """After the refinement loop, so it labels the final ranked set once; before
    SYNTHESIZE, which carries the labels to VALIDATE."""
    order = list(StageName)
    assert order.index(StageName.FULL_TEXT) < order.index(StageName.APPRAISE)
    assert order.index(StageName.APPRAISE) < order.index(StageName.SYNTHESIZE)


# --- what the models see ---------------------------------------------------------------


def test_the_appraisal_prompt_lists_every_handle_with_brackets_made_round() -> None:
    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    payload = AppraisalInput(
        product="Vitamin C",
        claim=CLAIM,
        sources=[
            {
                "handle": e.citation_handle,
                "title": e.paper.title,
                "abstract": e.paper.abstract,
                "study_type": e.paper.study_type,
            }
            for e in ctx.ranked[CLAIM]
        ],
    )

    prompt = build_appraisal_user_prompt(payload)

    assert "label each of S1, S2" in prompt
    assert "(0.94, 1.00)" in prompt and "[0.94" not in prompt
    assert prompt.rstrip().endswith(CLAIM)


def test_the_appraisal_grammar_carries_no_developer_rationale() -> None:
    """Same trap as the synthesis grammar: a docstring is prompt text."""
    rendered = json.dumps(AppraisalOutput.model_json_schema())

    for leaked in ("AppraiseStage", "not appraised", "Not measured", ".py"):
        assert leaked not in rendered


def test_labels_reach_validation_but_never_the_synthesis_prompt() -> None:
    """Hidden on purpose: two independent readings are what make a disagreement
    visible."""
    ctx = _ctx(prevents_the_common_cold=["a", "b"]).model_copy(
        update={"stances": {CLAIM: {"a": Stance.NO_EFFECT, "b": Stance.CONTRADICTS}}}
    )

    payload = SynthesizeStage._build_input(ctx)
    bare = payload.model_copy(
        update={"sources": [s.model_copy(update={"stances": {}}) for s in payload.sources]}
    )

    assert [s.stances for s in payload.sources] == [
        {CLAIM: Stance.NO_EFFECT},
        {CLAIM: Stance.CONTRADICTS},
    ]
    assert render_source_block(payload) == render_source_block(bare)


# --- the warning in VALIDATE ---------------------------------------------------------


def _payload(stances: dict[str, Stance | None], study_type: StudyType = StudyType.RCT):
    sources = [
        PromptSource(
            handle=handle,
            title=f"Trial {handle}",
            abstract="An abstract.",
            journal=None,
            year=2020,
            study_type=study_type,
            source_id=f"src-{handle}",
            claims=[CLAIM],
            stances={CLAIM: stance} if stance is not None else {},
        )
        for handle, stance in stances.items()
    ]
    return SynthesisInput(product="Vitamin C", target_claims=[CLAIM], sources=sources)


def _rows(payload: SynthesisInput) -> FakeSession:
    return FakeSession(
        [FakeSource(s.source_id, study_type=s.study_type) for s in payload.sources]
    )


async def test_supported_over_null_trials_passes_with_a_warning() -> None:
    """The gap the labels expose: today's cap counts these two trials toward
    `supported` because they were retrieved for the claim. Recorded and shown;
    the draft is not blocked while the labels' accuracy is unmeasured."""
    payload = _payload({"S1": Stance.NO_EFFECT, "S2": Stance.NO_EFFECT})
    draft = make_draft(verdict=Verdict.SUPPORTED, beat_2="Two trials were run [S1][S2].")

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert report.passed
    assert [w.code for w in report.warnings] == ["verdict_against_stance"]
    assert "S1, S2" in report.warnings[0].message
    assert report.stance_tally == {CLAIM: {"for": 0, "against": 2, "unlabelled": 0}}


async def test_a_refuted_claim_written_up_as_untested_is_flagged() -> None:
    """The measured failure: drafts refuting a claim read `no_evidence` while
    citing the trials that refuted it."""
    payload = _payload({"S1": Stance.NO_EFFECT, "S2": Stance.CONTRADICTS})
    draft = make_draft(verdict=Verdict.WEAK, beat_2="Two trials found nothing [S1][S2].")

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert report.passed
    assert "refutation_understated" in [w.code for w in report.warnings]


async def test_a_verdict_resting_only_on_off_topic_sources_is_flagged() -> None:
    """The measured failure: `supported`, citing three meta-analyses APPRAISE
    called off-topic, with a tally of 0 for and 0 against that read as no
    conflict at all."""
    payload = _payload(
        {"S3": Stance.OFF_TOPIC, "S11": Stance.OFF_TOPIC},
        study_type=StudyType.META_ANALYSIS,
    )
    draft = make_draft(
        verdict=Verdict.SUPPORTED,
        beat_2="Two reviews found it [S3][S11].",
        citations=[CitationGroup(claim=CLAIM, source_ids=["S3", "S11"])],
    )

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert report.passed
    (warning,) = [w for w in report.warnings if w.code == "verdict_on_off_topic_sources"]
    assert "S3, S11" in warning.message
    assert warning.detail["cited"] == {CLAIM: ["S3", "S11"]}
    assert report.stance_tally == {CLAIM: {"for": 0, "against": 0, "unlabelled": 0}}


async def test_one_on_topic_citation_is_enough_to_stay_quiet() -> None:
    payload = _payload({"S1": Stance.OFF_TOPIC, "S2": Stance.UNCLEAR})
    draft = make_draft(verdict=Verdict.WEAK, beat_2="Two trials were run [S1][S2].")

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert report.warnings == []


async def test_an_unappraised_citation_is_not_read_as_off_topic() -> None:
    """A missing label means nobody looked, never `off_topic`."""
    payload = _payload({"S1": Stance.OFF_TOPIC, "S2": None})
    draft = make_draft(verdict=Verdict.SUPPORTED, beat_2="Two trials were run [S1][S2].")

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert report.warnings == []


async def test_a_verdict_that_agrees_with_its_sources_is_left_alone() -> None:
    payload = _payload({"S1": Stance.SUPPORTS, "S2": Stance.SUPPORTS})
    draft = make_draft(verdict=Verdict.SUPPORTED, beat_2="Two trials agree [S1][S2].")

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert report.passed and report.warnings == []


async def test_nothing_appraised_is_not_recorded_rather_than_no_conflict() -> None:
    payload = _payload({"S1": None, "S2": None})
    draft = make_draft(verdict=Verdict.SUPPORTED, beat_2="Two trials were run [S1][S2].")

    report = await validate_draft(_rows(payload), draft, payload)  # type: ignore[arg-type]

    assert report.stance_tally is None
    assert report.warnings == []


def test_only_supported_tier_sources_are_counted_in_either_direction() -> None:
    """A confident verdict either way could only rest on these, so an animal
    study finding nothing is no more a refutation than one finding something
    is support."""
    tally = strong_by_direction(
        [
            (StudyType.RCT, Stance.SUPPORTS),
            (StudyType.META_ANALYSIS, Stance.CONTRADICTS),
            (StudyType.ANIMAL, Stance.NO_EFFECT),
            (StudyType.SYSTEMATIC_REVIEW, Stance.UNCLEAR),
            (StudyType.RCT, None),
        ]
    )

    assert tally == {"for": 1, "against": 1, "unlabelled": 1}


# --- the evaluation metric -----------------------------------------------------------


def _trace(stances: dict[str, str]) -> dict:
    return {
        "stages": [{"stage": "appraise", "metrics": {"cause": None}}],
        "rounds": [
            {
                "ranked": {
                    CLAIM: [
                        {"source_id": sid, "handle": f"S{i}"}
                        for i, sid in enumerate(["a", "b", "c"], 1)
                    ]
                },
                "candidates": {},
            }
        ],
        "papers": {
            sid: {"source_id": sid, "pmid": pmid, "title": sid}
            for sid, pmid in [("a", "1"), ("b", "2"), ("c", "3")]
        },
        "stances": {CLAIM: stances},
        "final_validation": {"stance_tally": {}, "warnings": []},
    }


def test_the_metric_scores_labelled_gold_papers_and_separates_direction() -> None:
    expected = {
        CLAIM: {"pmid:1": "no_effect", "pmid:2": "no_effect", "pmid:3": "supports"},
        # A claim this run's extraction did not produce: reported, not scored.
        "cures colds": {"pmid:1": "no_effect"},
    }

    result = stance_metrics.evaluate(
        _trace({"a": "no_effect", "b": "contradicts"}), expected
    )

    assert (result["offered"], result["labelled"]) == (3, 2)
    # c was ranked but never labelled: neither right nor wrong.
    assert (result["scored"], result["unlabelled_gold"]) == (2, 1)
    assert result["accuracy"] == 0.5
    # contradicts for no_effect is the right direction, only the wrong label.
    assert result["direction_accuracy"] == 1.0
    assert result["disagrees"] is False
    assert result["unmatched_claims"] == ["cures colds"]


def test_a_label_is_scored_only_against_its_own_claim() -> None:
    """The same paper can point different ways for two claims; a label for
    one must never be graded against the other's."""
    trace = _trace({"a": "supports"})
    expected = {"is bad for heart health": {"pmid:1": "no_effect"}}

    assert stance_metrics.evaluate(trace, expected)["scored"] == 0


def test_the_metric_does_not_apply_when_appraise_never_ran() -> None:
    trace = _trace({})
    trace["stages"] = []

    assert stance_metrics.evaluate(trace, {}) == {"applicable": False}


def test_labels_do_not_change_the_synthesis_recording_key() -> None:
    """The model is not shown them, so a recording made before APPRAISE existed
    must still replay rather than re-run every draft live."""
    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    bare = SynthesizeStage._build_input(ctx)
    labelled = SynthesizeStage._build_input(
        ctx.model_copy(update={"stances": {CLAIM: {"a": Stance.NO_EFFECT}}})
    )

    assert labelled.sources[0].stances
    assert synthesis_key("m", labelled, None) == synthesis_key("m", bare, None)


def test_an_answer_with_no_labels_is_refused_so_the_repair_retry_runs() -> None:
    """Measured: the model wrote the claim's outcome and stopped. Refused here,
    an empty answer goes back through the one repair retry instead of quietly
    leaving every source unappraised."""
    with pytest.raises(ValidationError):
        AppraisalOutput(claim_outcome="liver detoxification", items=[])
    schema = AppraisalOutput.model_json_schema()
    assert schema["properties"]["items"]["minItems"] == 1


# --- APPRAISAL_MODE=two_call ------------------------------------------------------


class _TwoCallClient:
    """Scripted per claim, handle -> stance. ``OFF_TOPIC`` means the relevance
    call rejects the source; anything else means relevance accepts it and the
    direction call gives that stance. An exception in place of a claim's script
    fails its relevance call; ``direction_fails`` fails only its direction call.
    """

    def __init__(
        self,
        script: dict[str, dict[str, Stance] | Exception],
        *,
        direction_fails: dict[str, Exception] | None = None,
    ) -> None:
        self._script = script
        self._direction_fails = direction_fails or {}
        self.relevance_calls: list[AppraisalInput] = []
        self.direction_calls: list[AppraisalInput] = []

    async def relevance(self, payload: AppraisalInput) -> LLMResult[RelevanceOutput]:
        self.relevance_calls.append(payload)
        answer = self._script[payload.claim]
        if isinstance(answer, Exception):
            raise answer
        items = [
            RelevanceItem(
                handle=handle,
                outcome_measured="how often colds happen",
                measures_claim_outcome=stance is not Stance.OFF_TOPIC,
            )
            for handle, stance in answer.items()
        ]
        return LLMResult(
            output=RelevanceOutput(claim_outcome="catching a cold", items=items),
            usage=TokenUsage(input_tokens=100, output_tokens=20),
            model=MODEL,
        )

    async def direction(self, payload: AppraisalInput) -> LLMResult[DirectionOutput]:
        self.direction_calls.append(payload)
        if (failure := self._direction_fails.get(payload.claim)) is not None:
            raise failure
        answer = self._script[payload.claim]
        assert isinstance(answer, dict)
        items = [
            DirectionItem(handle=s.handle, finding="It found this.", stance=answer[s.handle])
            for s in payload.sources
        ]
        return LLMResult(
            output=DirectionOutput(items=items),
            usage=TokenUsage(input_tokens=60, output_tokens=20),
            model=MODEL,
        )


def _handles_of(calls: list[AppraisalInput]) -> list[list[str]]:
    return [[s.handle for s in call.sources] for call in calls]




async def test_two_call_each_pair_gets_its_label_keyed_by_claim_and_source() -> None:
    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    client = _TwoCallClient({CLAIM: {"S1": Stance.NO_EFFECT, "S2": Stance.SUPPORTS}})

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert result.stances == {CLAIM: {"a": Stance.NO_EFFECT, "b": Stance.SUPPORTS}}
    metrics = result.metrics[StageName.APPRAISE]
    assert metrics["cause"] is None
    assert (metrics["offered"], metrics["labelled"], metrics["missing"]) == (2, 2, 0)
    assert metrics["calls"] == 2
    assert metrics["by_stance"] == {"no_effect": 1, "supports": 1}
    # Both calls are on the ledger.
    assert result.usage_by_stage[StageName.APPRAISE].usage.input_tokens == 160


async def test_two_call_a_study_ruled_off_topic_is_never_asked_for_a_direction() -> None:
    """The measured failure the second call exists for: asked for a direction,
    a model gives one to a study of something else ("colds were shorter" read
    as evidence about catching them). Not shown, it cannot."""
    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    client = _TwoCallClient({CLAIM: {"S1": Stance.OFF_TOPIC, "S2": Stance.NO_EFFECT}})

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert _handles_of(client.relevance_calls) == [["S1", "S2"]]
    assert _handles_of(client.direction_calls) == [["S2"]]
    assert result.stances == {CLAIM: {"a": Stance.OFF_TOPIC, "b": Stance.NO_EFFECT}}
    assert result.metrics[StageName.APPRAISE]["ruled_off_topic"] == 1


async def test_two_call_nothing_relevant_skips_the_direction_call() -> None:
    """A no-evidence topic — twelve abstracts about everything but the claim —
    costs one call, not two."""
    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    client = _TwoCallClient({CLAIM: {"S1": Stance.OFF_TOPIC, "S2": Stance.OFF_TOPIC}})

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert client.direction_calls == []
    assert result.stances == {CLAIM: {"a": Stance.OFF_TOPIC, "b": Stance.OFF_TOPIC}}
    metrics = result.metrics[StageName.APPRAISE]
    assert (metrics["calls"], metrics["direction_skipped"]) == (1, 1)


async def test_two_call_each_claim_is_asked_about_its_own_sources_only() -> None:
    """A label is about a (source, claim) pair: a paper retrieved for two claims
    can point different ways on each."""
    ctx = _ctx(prevents_the_common_cold=["a", "b"], shortens_colds=["b"])
    client = _TwoCallClient(
        {
            CLAIM: {"S1": Stance.NO_EFFECT, "S2": Stance.NO_EFFECT},
            OTHER: {"S2": Stance.SUPPORTS},
        }
    )

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert _handles_of(client.relevance_calls) == [["S1", "S2"], ["S2"]]
    assert result.stances[CLAIM]["b"] is Stance.NO_EFFECT
    assert result.stances[OTHER]["b"] is Stance.SUPPORTS


async def test_two_call_a_skipped_handle_is_not_appraised_rather_than_unclear() -> None:
    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    client = _TwoCallClient({CLAIM: {"S1": Stance.NO_EFFECT}})

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert result.stances == {CLAIM: {"a": Stance.NO_EFFECT}}
    assert result.metrics[StageName.APPRAISE]["missing"] == 1


async def test_two_call_an_invented_handle_is_dropped_never_guessed_onto_a_source() -> None:
    ctx = _ctx(prevents_the_common_cold=["a"])
    client = _TwoCallClient({CLAIM: {"S1": Stance.SUPPORTS, "S9": Stance.NO_EFFECT}})

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert _handles_of(client.direction_calls) == [["S1"]]
    assert result.stances == {CLAIM: {"a": Stance.SUPPORTS}}
    assert result.metrics[StageName.APPRAISE]["invented"] == 1


async def test_two_call_a_direction_for_a_source_it_was_not_shown_is_dropped() -> None:
    """Relevance outranks direction: a source ruled off topic stays off topic
    even if the direction call names it anyway."""

    class _Talkative(_TwoCallClient):
        async def direction(self, payload: AppraisalInput) -> LLMResult[DirectionOutput]:
            result = await super().direction(payload)
            extra = DirectionItem(
                handle="S1", finding="Colds were shorter.", stance=Stance.SUPPORTS
            )
            return LLMResult(
                output=DirectionOutput(items=[*result.output.items, extra]),
                usage=result.usage,
                model=result.model,
            )

    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    client = _Talkative({CLAIM: {"S1": Stance.OFF_TOPIC, "S2": Stance.NO_EFFECT}})

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert result.stances == {CLAIM: {"a": Stance.OFF_TOPIC, "b": Stance.NO_EFFECT}}
    assert result.metrics[StageName.APPRAISE]["invented"] == 1


async def test_two_call_a_failed_relevance_call_costs_only_its_claim_and_is_billed() -> None:
    """The stage never fails a run, and a failed call that the provider answered
    is billed like a success."""
    failure = LLMError(
        "appraisal relevance: output still failed validation",
        usage=TokenUsage(input_tokens=50, output_tokens=10),
        model=MODEL,
    )
    ctx = _ctx(prevents_the_common_cold=["a"], shortens_colds=["b"])
    client = _TwoCallClient({CLAIM: failure, OTHER: {"S2": Stance.SUPPORTS}})

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert result.stances == {OTHER: {"b": Stance.SUPPORTS}}
    metrics = result.metrics[StageName.APPRAISE]
    assert (metrics["cause"], metrics["failed_calls"]) == ("partial_model_error", 1)
    assert result.usage_by_stage[StageName.APPRAISE].usage.input_tokens == 50 + 100 + 60


async def test_two_call_a_failed_direction_call_keeps_the_off_topic_rulings() -> None:
    """Those labels came from a call that succeeded; only the relevant sources,
    which had no direction yet, go unappraised."""
    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    client = _TwoCallClient(
        {CLAIM: {"S1": Stance.OFF_TOPIC, "S2": Stance.SUPPORTS}},
        direction_fails={CLAIM: LLMError("timed out", retryable=True)},
    )

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert result.stances == {CLAIM: {"a": Stance.OFF_TOPIC}}
    metrics = result.metrics[StageName.APPRAISE]
    assert (metrics["cause"], metrics["missing"]) == ("partial_model_error", 1)


async def test_two_call_every_call_failing_is_recorded_not_raised() -> None:
    ctx = _ctx(prevents_the_common_cold=["a"])
    client = _TwoCallClient({CLAIM: LLMError("is `ollama serve` running?", retryable=True)})

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert result.stances == {}
    assert result.metrics[StageName.APPRAISE]["cause"] == "model_error"


async def test_two_call_an_unexpected_error_keeps_the_claims_already_labelled() -> None:
    ctx = _ctx(prevents_the_common_cold=["a"], shortens_colds=["b"])
    client = _TwoCallClient({CLAIM: {"S1": Stance.NO_EFFECT}, OTHER: RuntimeError("a bug")})

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert result.stances == {CLAIM: {"a": Stance.NO_EFFECT}}
    assert result.metrics[StageName.APPRAISE]["cause"] == "unexpected"


async def test_two_call_switched_off_it_makes_no_call() -> None:
    result = await AppraiseStage(None).run(_ctx(prevents_the_common_cold=["a"]))

    assert result.stances == {}
    assert result.metrics[StageName.APPRAISE]["cause"] == "disabled"


async def test_two_call_nothing_ranked_means_nothing_to_label() -> None:
    client = _TwoCallClient({})
    ctx = _ctx(prevents_the_common_cold=["a"]).model_copy(update={"ranked": {CLAIM: []}})

    result = await AppraiseStage(client, two_call=True).run(ctx)

    assert client.relevance_calls == []
    assert result.metrics[StageName.APPRAISE]["cause"] == "no_sources"


def _appraisal_input() -> AppraisalInput:
    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    return AppraisalInput(
        product="Vitamin C",
        claim=CLAIM,
        sources=[
            AppraisalSource(
                handle=e.citation_handle,
                title=e.paper.title,
                abstract=e.paper.abstract,
                study_type=e.paper.study_type,
            )
            for e in ctx.ranked[CLAIM]
        ],
    )


@pytest.mark.parametrize("build", [build_relevance_user_prompt, build_direction_user_prompt])
def test_both_prompts_list_every_handle_with_brackets_made_round(build) -> None:  # type: ignore[no-untyped-def]
    prompt = build(_appraisal_input())

    assert "each of S1, S2" in prompt
    assert "(0.94, 1.00)" in prompt and "[0.94" not in prompt
    assert prompt.rstrip().endswith(CLAIM)


@pytest.mark.parametrize("output", [RelevanceOutput, DirectionOutput])
def test_neither_grammar_carries_developer_rationale(output) -> None:  # type: ignore[no-untyped-def]
    """Same trap as the synthesis grammar: a docstring is prompt text."""
    rendered = json.dumps(output.model_json_schema())

    # Dated notes ("measured 2026-10-02") are written for maintainers.
    for leaked in ("AppraiseStage", "not appraised", "2026", "flipped", ".py"):
        assert leaked not in rendered


def test_two_call_answers_with_no_labels_are_refused() -> None:
    """Measured: the model wrote the claim's outcome and stopped. Refused here,
    an empty answer goes back through the one repair retry instead of quietly
    leaving every source unappraised."""
    with pytest.raises(ValidationError):
        RelevanceOutput(claim_outcome="liver detoxification", items=[])
    with pytest.raises(ValidationError):
        DirectionOutput(items=[])
    for output in (RelevanceOutput, DirectionOutput):
        assert output.model_json_schema()["properties"]["items"]["minItems"] == 1


# --- the outcome guard on refutations (one_call) -------------------------------------


def test_a_refutation_quoting_a_different_outcome_is_ruled_off_topic() -> None:
    """Measured: "increases in strength" quoted and labelled no_effect for a claim
    about endurance — a made-up refutation, the commonest directional error left."""
    strength = _item(
        "S1", Stance.NO_EFFECT, quote="RT plus creatine significantly increased strength."
    )
    assert label(strength, "muscle endurance") is Stance.OFF_TOPIC
    contradicting = _item("S1", Stance.CONTRADICTS, quote="Mood got worse.")
    assert label(contradicting, "memory") is Stance.OFF_TOPIC


def test_a_refutation_about_the_outcome_stands() -> None:
    real = _item("S1", Stance.NO_EFFECT, quote="Colds were no less frequent (RR 0.97).")
    assert label(real, "how often colds happen") is Stance.NO_EFFECT
    # Up to a suffix: latency / latencies.
    latency = _item("S2", Stance.NO_EFFECT, quote="Sleep onset latencies did not change.")
    assert label(latency, "sleep latency") is Stance.NO_EFFECT


def test_support_is_never_held_to_the_word_test() -> None:
    """Synonyms ("TG" for triglycerides) would turn real support into a false
    off-topic call; only the label a refutation would be built from is guarded."""
    tg = _item("S1", Stance.SUPPORTS, quote="Supplementation reduced TG by 15%.")
    assert label(tg, "triglycerides") is Stance.SUPPORTS


def test_an_outcome_of_only_filler_words_cannot_be_tested() -> None:
    vague = _item("S1", Stance.NO_EFFECT, quote="Nothing changed.")
    assert label(vague, "how often") is Stance.NO_EFFECT


async def test_guard_overrides_are_counted_apart_from_relevance_overrides() -> None:
    class _Strength(_Client):
        async def appraise(self, payload: AppraisalInput) -> LLMResult[AppraisalOutput]:
            items = [
                _item("S1", Stance.NO_EFFECT, quote="Strength increased."),
                _item("S2", Stance.SUPPORTS, measured=False),
            ]
            output = AppraisalOutput(claim_outcome="muscle endurance", items=items)
            return LLMResult(output=output, usage=TokenUsage())

    ctx = _ctx(prevents_the_common_cold=["a", "b"])
    result = await AppraiseStage(_Strength({})).run(ctx)

    assert result.stances == {CLAIM: {"a": Stance.OFF_TOPIC, "b": Stance.OFF_TOPIC}}
    metrics = result.metrics[StageName.APPRAISE]
    assert (metrics["outcome_guard_overrides"], metrics["relevance_overrides"]) == (1, 1)
