"""The synthesis prompt is cut to fit the generation server's window.

2026-10-25: GLP-1 side effects built a 44,566-token prompt against llama-server's
32,768 and the run failed permanently. Excerpts go before sources, worst-ranked
first, and a prompt that already fits is untouched.
"""

from __future__ import annotations

from app.domain.contracts import Excerpt, PromptSource, SynthesisInput
from app.domain.enums import StudyType
from app.pipeline.steps import synthesize
from app.pipeline.steps.synthesize import (
    SYNTHESIS_RESERVED_TOKENS,
    _fit_context,
    _prompt_tokens,
)

CLAIM = "reduces weight"


def _source(n: int, words: int, excerpt_words: int = 0) -> PromptSource:
    return PromptSource(
        handle=f"S{n}",
        title=f"Paper {n}",
        abstract="word " * words,
        journal=None,
        year=2025,
        study_type=StudyType.RCT,
        source_id=f"id-{n}",
        claims=[CLAIM],
        excerpts=[Excerpt(section="Results", text="word " * excerpt_words)]
        if excerpt_words
        else [],
    )


def _payload(*sources: PromptSource) -> SynthesisInput:
    return SynthesisInput(product="Semaglutide", target_claims=[CLAIM], sources=list(sources))


def test_a_prompt_that_fits_is_left_alone() -> None:
    payload = _payload(_source(1, 200), _source(2, 200))
    fitted, metrics = _fit_context(payload, {"S1": 0, "S2": 1})
    assert fitted is payload and metrics is None


def test_excerpts_go_first_then_the_worst_ranked_sources(monkeypatch) -> None:
    # Room for the instructions plus about two sources without excerpts.
    base = _prompt_tokens(_payload(_source(1, 1)))
    monkeypatch.setattr(
        synthesize, "SYNTHESIS_CONTEXT_TOKENS", SYNTHESIS_RESERVED_TOKENS + base + 1_500
    )
    payload = _payload(*(_source(n, 600, excerpt_words=600) for n in range(1, 7)))
    positions = {f"S{n}": n - 1 for n in range(1, 7)}  # S1 ranked best

    fitted, metrics = _fit_context(payload, positions)

    assert metrics is not None
    assert metrics["excerpts_dropped"][0] == "S6"  # worst-ranked excerpt first
    assert metrics["estimated_tokens_after"] <= metrics["budget_tokens"]
    kept = [s.handle for s in fitted.sources]
    assert kept[0] == "S1" and not set(metrics["sources_dropped"]) & set(kept)
    # Sources went, and only after every excerpt had.
    assert metrics["sources_dropped"][0] == "S6"
    assert len(metrics["excerpts_dropped"]) == 6


def test_the_last_source_is_never_dropped(monkeypatch) -> None:
    base = _prompt_tokens(_payload(_source(1, 1)))
    window = SYNTHESIS_RESERVED_TOKENS + base
    monkeypatch.setattr(synthesize, "SYNTHESIS_CONTEXT_TOKENS", window)
    fitted, metrics = _fit_context(_payload(_source(1, 5_000), _source(2, 5_000)), {})
    assert metrics is not None and len(fitted.sources) == 1
