"""``synthesis_relevance_filter``: APPRAISE's relevance calls trim the prompt.

Off by default and under measurement (config.py). What must hold either way:
direction never reaches the prompt, the quorum's ``claims`` is untouched, the
filter never empties a payload, and switching it off leaves the eval's
recordings replayable.
"""

from __future__ import annotations

from app.domain.contracts import PromptSource, SynthesisInput
from app.domain.enums import Stance, StudyType
from app.llm.prompts.synthesis import render_source_block
from app.pipeline.steps.synthesize import _filter_by_relevance
from evaluation.harness.llm import synthesis_key

MASS, ENDURANCE = "increases muscle mass", "improves muscle endurance"


def _source(n: int, stances: dict[str, Stance]) -> PromptSource:
    return PromptSource(
        handle=f"S{n}",
        title=f"Paper {n}",
        abstract="An abstract.",
        journal=None,
        year=2025,
        study_type=StudyType.META_ANALYSIS,
        source_id=f"id-{n}",
        claims=[MASS, ENDURANCE],
        stances=stances,
    )


def _payload(*sources: PromptSource) -> SynthesisInput:
    return SynthesisInput(
        product="Creatine", target_claims=[MASS, ENDURANCE], sources=list(sources)
    )


def test_a_source_off_topic_for_every_claim_is_left_out() -> None:
    payload = _payload(
        _source(1, {MASS: Stance.OFF_TOPIC, ENDURANCE: Stance.OFF_TOPIC}),
        _source(2, {MASS: Stance.SUPPORTS}),
    )

    trimmed, metrics = _filter_by_relevance(payload)

    assert [s.handle for s in trimmed.sources] == ["S2"]
    assert metrics["dropped_handles"] == ["S1"]


def test_a_partly_relevant_source_names_only_its_relevant_claims() -> None:
    """The run-baa0e155 decoy: a keto paper shown as retrieved for all three."""
    payload = _payload(_source(1, {MASS: Stance.NO_EFFECT, ENDURANCE: Stance.OFF_TOPIC}))

    trimmed, _ = _filter_by_relevance(payload)
    rendered = render_source_block(trimmed)

    assert f"Retrieved for: {MASS}\n" in rendered
    assert trimmed.sources[0].claims == [MASS, ENDURANCE]  # the quorum's view


def test_an_unappraised_claim_counts_as_relevant() -> None:
    trimmed, _ = _filter_by_relevance(_payload(_source(1, {})))

    assert trimmed.sources[0].shown_claims == [MASS, ENDURANCE]


def test_the_filter_never_empties_a_payload() -> None:
    """Empty would take the no-evidence template on the labels' word alone."""
    payload = _payload(_source(1, {MASS: Stance.OFF_TOPIC, ENDURANCE: Stance.OFF_TOPIC}))

    trimmed, metrics = _filter_by_relevance(payload)

    assert trimmed == payload
    assert metrics["skipped"] == "all_off_topic"


def test_the_rendered_prompt_never_shows_a_direction() -> None:
    payload = _payload(_source(1, {MASS: Stance.CONTRADICTS, ENDURANCE: Stance.SUPPORTS}))

    rendered = render_source_block(_filter_by_relevance(payload)[0])

    for stance in Stance:
        assert stance.value not in rendered


def test_an_unfiltered_payload_keeps_its_recorded_cassette_key() -> None:
    """Flag off must replay the recordings made before the field existed."""
    payload = _payload(_source(1, {MASS: Stance.SUPPORTS}))
    without_field = payload.model_dump(mode="json")
    for source in without_field["sources"]:
        del source["shown_claims"]

    assert synthesis_key("m", payload, None) == synthesis_key(
        "m", SynthesisInput.model_validate(without_field), None
    )
    narrowed = _filter_by_relevance(payload)[0]
    assert synthesis_key("m", narrowed, None) != synthesis_key("m", payload, None)
