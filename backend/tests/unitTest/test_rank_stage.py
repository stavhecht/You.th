"""``RankStage`` — reads what retrieval cached, writes nothing.

The stage had no tests while it was doing the thing fix #5 removed: re-upserting
the whole candidate set on every run to recover row ids the previous stage
already held. Two kinds of guard here.

Behavioural ones, driven by a fake reranker so no database is needed: the stage
works from ``ctx.candidates`` alone, handles are assigned once across the whole
article, and an empty pool passes through rather than raising — the fringe-trend
path that has to reach synthesis for the ``no evidence`` article to exist.

And one structural one, in the spirit of ``test_publish_path.py``: the stage
must stay free of the cache. That is the regression a behavioural test cannot
see coming, because re-adding the lookup would make every test here pass.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.domain.contracts import (
    CachedCandidate,
    CandidatePaper,
    ExtractionOutput,
    RankedSource,
)
from app.domain.enums import SourceApi, StudyType
from app.pipeline.stages import PipelineContext, StageError, StageName
from app.pipeline.steps import rank as rank_module
from app.pipeline.steps.rank import RankStage, thin_claims
from app.retrieval.rerank import RerankConfig

RANK_SOURCE = Path(inspect.getfile(rank_module))


def _candidate(source_id: str, study_type: StudyType = StudyType.RCT) -> CachedCandidate:
    return CachedCandidate(
        source_id=source_id,
        paper=CandidatePaper(
            pmid=source_id.removeprefix("src-"),
            title=f"Trial {source_id}",
            abstract="An abstract long enough to be worth embedding and reading.",
            study_type=study_type,
            year=2024,
            source_api=SourceApi.PUBMED,
        ),
    )


class _ScoredReranker:
    """Scores by position, so ordering assertions are deterministic.

    Records what it was handed, which is how these tests check that the stage
    passes the context's candidates straight through instead of resolving them
    somewhere else first.
    """

    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self._scores = scores or {}
        self.seen: dict[str, list[str]] = {}
        self.subjects: dict[str, str | None] = {}

    async def rank_for_claim(
        self,
        claim: str,
        candidates: list[CachedCandidate],
        config: RerankConfig | None = None,
        *,
        subject: str | None = None,
    ) -> list[RankedSource]:
        self.seen[claim] = [entry.source_id for entry in candidates]
        self.subjects[claim] = subject
        return [
            RankedSource(
                source_id=entry.source_id,
                claim=claim,
                citation_handle="S0",
                paper=entry.paper,
                cosine_similarity=self._scores.get(entry.source_id, 0.5),
                final_score=self._scores.get(entry.source_id, 0.5),
            )
            for entry in candidates
        ]


def _stage(reranker: _ScoredReranker) -> RankStage:
    return RankStage(reranker, RerankConfig())  # type: ignore[arg-type]


def _ctx(candidates: dict[str, list[CachedCandidate]]) -> PipelineContext:
    return PipelineContext(
        run_id="run-1",
        topic="ashwagandha",
        extraction=ExtractionOutput(
            product="Ashwagandha capsules",
            target_claims=list(candidates) or ["reduces stress"],
            ingredients=["ashwagandha"],
        ),
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# Working from the context alone.
# ---------------------------------------------------------------------------


async def test_candidates_reach_the_reranker_exactly_as_retrieval_left_them() -> None:
    """No resolution step in between. The ids are already on the context, which
    is the whole point of ``CachedCandidate``."""
    reranker = _ScoredReranker()
    ctx = _ctx(
        {
            "reduces stress": [_candidate("src-1"), _candidate("src-2")],
            "improves sleep": [_candidate("src-3")],
        }
    )

    await _stage(reranker).run(ctx)

    assert reranker.seen == {
        "reduces stress": ["src-1", "src-2"],
        "improves sleep": ["src-3"],
    }


async def test_every_claim_is_ranked_even_one_with_a_single_candidate() -> None:
    reranker = _ScoredReranker()
    ctx = _ctx({"a": [_candidate("src-1")], "b": [_candidate("src-2")]})

    result = await _stage(reranker).run(ctx)

    assert set(result.ranked) == {"a", "b"}
    assert [e.source_id for e in result.ranked["a"]] == ["src-1"]


async def test_an_empty_candidate_pool_is_not_an_error() -> None:
    """The fringe-trend path. Raising here would stop the ``no evidence``
    article from ever being written — see ``services/no_evidence.py``."""
    result = await _stage(_ScoredReranker()).run(_ctx({"reduces stress": []}))

    assert result.ranked == {"reduces stress": []}


async def test_no_candidates_at_all_still_produces_a_context() -> None:
    result = await _stage(_ScoredReranker()).run(_ctx({}))

    assert result.ranked == {}


async def test_a_missing_extraction_is_a_stage_error() -> None:
    """Ordering guard: ranking a claim set that was never extracted would
    silently rank nothing and look like a topic with no literature."""
    ctx = PipelineContext(run_id="run-1", topic="ashwagandha")

    with pytest.raises(StageError):
        await _stage(_ScoredReranker()).run(ctx)


# ---------------------------------------------------------------------------
# Handles, assigned once across the article.
# ---------------------------------------------------------------------------


async def test_a_source_answering_two_claims_carries_one_handle() -> None:
    """The stage-level version of the guard in ``test_rerank_scoring.py``.

    Because ``ctx.candidates`` now carries ids from retrieval, a paper answering
    two claims arrives under both with the same id — so this property depends on
    the pairing being consistent, not just on ``assign_handles``.
    """
    reranker = _ScoredReranker({"src-shared": 0.9, "src-other": 0.4})
    ctx = _ctx(
        {
            "reduces stress": [_candidate("src-shared")],
            "improves sleep": [_candidate("src-shared"), _candidate("src-other")],
        }
    )

    result = await _stage(reranker).run(ctx)

    handles = {
        entry.source_id: entry.citation_handle
        for ranked in result.ranked.values()
        for entry in ranked
    }
    assert handles == {"src-shared": "S1", "src-other": "S2"}


async def test_handles_are_numbered_strongest_first_across_all_claims() -> None:
    """S1 must be the best evidence in the article, not the best in whichever
    claim came first — the model leans on earlier handles."""
    reranker = _ScoredReranker({"src-weak": 0.1, "src-strong": 0.95, "src-mid": 0.5})
    ctx = _ctx(
        {
            "first claim": [_candidate("src-weak")],
            "second claim": [_candidate("src-strong"), _candidate("src-mid")],
        }
    )

    result = await _stage(reranker).run(ctx)

    handles = {
        entry.source_id: entry.citation_handle
        for ranked in result.ranked.values()
        for entry in ranked
    }
    assert handles == {"src-strong": "S1", "src-mid": "S2", "src-weak": "S3"}


async def test_no_placeholder_handle_survives_the_stage() -> None:
    """``rank_for_claim`` emits S0 before assignment. One reaching a prompt is a
    handle the model can cite and validation cannot resolve."""
    reranker = _ScoredReranker()
    ctx = _ctx({"a": [_candidate("src-1"), _candidate("src-2")]})

    result = await _stage(reranker).run(ctx)

    assert all(
        entry.citation_handle != "S0" for ranked in result.ranked.values() for entry in ranked
    )


# ---------------------------------------------------------------------------
# Metrics — the early warning on a capped verdict.
# ---------------------------------------------------------------------------


async def test_metrics_report_the_kept_set_per_claim_and_its_study_types() -> None:
    """An all-in-vitro kept set means the verdict is about to be capped at
    'weak'. Far easier to read here than to reverse-engineer from a validation
    failure two stages later."""
    reranker = _ScoredReranker()
    ctx = _ctx(
        {
            "a": [
                _candidate("src-1", StudyType.IN_VITRO),
                _candidate("src-2", StudyType.IN_VITRO),
            ],
            "b": [_candidate("src-3", StudyType.META_ANALYSIS)],
        }
    )

    metrics = (await _stage(reranker).run(ctx)).metrics[StageName.RANK]

    assert metrics["kept_per_claim"] == {"a": 2, "b": 1}
    assert metrics["study_types"] == {"in_vitro": 2, "meta_analysis": 1}


async def test_unique_sources_counts_a_shared_paper_once() -> None:
    """It is the size of the evidence base, not the number of citations.

    Counting a shared paper twice would report two sources where one exists,
    which is the same overstatement ``dedup.py`` and ``assign_handles`` guard
    from their own ends.
    """
    reranker = _ScoredReranker()
    ctx = _ctx(
        {
            "a": [_candidate("src-shared")],
            "b": [_candidate("src-shared"), _candidate("src-other")],
        }
    )

    metrics = (await _stage(reranker).run(ctx)).metrics[StageName.RANK]

    assert metrics["unique_sources"] == 2


# ---------------------------------------------------------------------------
# Structural: the stage must stay free of the cache.
# ---------------------------------------------------------------------------


def test_the_stage_takes_no_source_cache() -> None:
    """The dependency fix #5 removed.

    A behavioural test cannot catch this coming back: re-adding the lookup would
    leave every test above passing while restoring ~100 statements per run
    against rows written seconds earlier.
    """
    parameters = set(inspect.signature(RankStage.__init__).parameters)

    assert parameters == {"self", "reranker", "config"}


def test_the_module_never_writes_to_the_cache() -> None:
    """Belt to the signature's braces: a cache reachable through the reranker,
    or constructed inline, would not show up in ``__init__``."""
    tree = ast.parse(RANK_SOURCE.read_text(), filename=str(RANK_SOURCE))

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "upsert_many" not in called
    assert "ensure_embeddings" not in called


# ---------------------------------------------------------------------------
# The thin-claim predicate that drives the refinement cycle.
# ---------------------------------------------------------------------------


def _ranked(claim: str, types: list[StudyType]) -> list[RankedSource]:
    return [
        RankedSource(
            source_id=f"id-{claim}-{index}",
            claim=claim,
            citation_handle=f"S{index + 1}",
            paper=CandidatePaper(
                pmid=str(7000 + index),
                title=f"Paper {index}",
                abstract="An abstract.",
                study_type=study_type,
                source_api=SourceApi.PUBMED,
            ),
            cosine_similarity=0.5,
            final_score=0.6,
        )
        for index, study_type in enumerate(types)
    ]


def test_a_claim_meeting_the_quorum_is_not_thin() -> None:
    ranked = _ranked("c", [StudyType.META_ANALYSIS, StudyType.SYSTEMATIC_REVIEW])

    assert thin_claims({"c": ranked}) == []


def test_a_claim_one_short_of_the_quorum_is_thin() -> None:
    """Its ceiling is already 'mixed' before the model writes a word, so the
    verdict was decided by what retrieval found rather than by the evidence."""
    ranked = _ranked("c", [StudyType.META_ANALYSIS, StudyType.OBSERVATIONAL])

    assert thin_claims({"c": ranked}) == ["c"]


def test_a_claim_with_many_weak_sources_is_still_thin() -> None:
    """The test is supported-tier count, not volume. Twelve narrative reviews
    cap the article exactly as firmly as one does."""
    ranked = _ranked("c", [StudyType.NARRATIVE_REVIEW] * 12)

    assert thin_claims({"c": ranked}) == ["c"]


def test_a_claim_with_no_sources_at_all_is_thin() -> None:
    assert thin_claims({"c": []}) == ["c"]


def test_only_the_thin_claims_are_named() -> None:
    good = _ranked("good", [StudyType.RCT, StudyType.RCT])
    bad = _ranked("bad", [StudyType.ANIMAL])

    assert thin_claims({"good": good, "bad": bad}) == ["bad"]


# ---------------------------------------------------------------------------
# Ranking against the subject, and outages.
# ---------------------------------------------------------------------------


async def test_claims_are_ranked_against_the_subject_the_search_used() -> None:
    """The bare claim names an outcome, not a substance; ranked on it, papers
    about the outcome alone beat papers about the substance. The subject is the
    one the query was anchored on: ingredients first, then the product."""
    reranker = _ScoredReranker()
    stage = RankStage(reranker, RerankConfig())  # type: ignore[arg-type]
    await stage.run(_ctx({"reduces stress": []}))
    assert reranker.subjects["reduces stress"] == "ashwagandha"


def test_the_ranking_text_leads_with_the_subject() -> None:
    from app.retrieval.rerank import claim_subject, ranking_text

    assert ranking_text("improves sleep quality", "magnesium") == (
        "magnesium improves sleep quality"
    )
    assert ranking_text("improves sleep quality", None) == "improves sleep quality"
    assert (
        claim_subject("Calm Blend", ["magnesium", "l-theanine"]) == "magnesium and l-theanine"
    )
    assert claim_subject("foam rolling", []) == "foam rolling"


@pytest.mark.parametrize("error", ["embedding", "database"])
async def test_an_embedding_or_database_outage_is_retryable(error: str) -> None:
    """Retrying an outage finds every paper already cached; failing it for good
    throws the run away over a server that was down for a minute."""
    from sqlalchemy.exc import OperationalError

    from app.llm.embeddings.base import EmbeddingError

    class _Down:
        async def rank_for_claim(self, *args: object, **kwargs: object) -> list[RankedSource]:
            if error == "embedding":
                raise EmbeddingError("ollama unreachable")
            raise OperationalError("SELECT 1", {}, ConnectionRefusedError("refused"))

    stage = RankStage(_Down(), RerankConfig())  # type: ignore[arg-type]
    with pytest.raises(StageError) as exc_info:
        await stage.run(_ctx({"reduces stress": [_candidate("a")]}))
    assert exc_info.value.retryable is True


async def test_a_broad_question_keeps_its_best_studied_claims() -> None:
    """wt_023b, 2026-10-25: six side-effect claims overflowed the prompt and the
    thinnest capped the verdict. The cut must narrow the claim list too, since
    VALIDATE seeds a quorum for every claim in it."""
    reviews = [_candidate(f"src-r{i}", StudyType.META_ANALYSIS) for i in range(3)]
    weak = [_candidate(f"src-w{i}", StudyType.OBSERVATIONAL) for i in range(5)]
    ctx = _ctx(
        {
            "causes nausea": reviews,
            "causes pancreatitis": reviews[:2],
            "causes hair loss": weak,  # many sources, none strong
            "causes gallbladder disease": reviews[:2] + weak[:1],
            "causes kidney injury": reviews,
            "causes injection site reactions": weak[:1],
        }
    )

    result = await _stage(_ScoredReranker()).run(ctx)

    kept = ["causes nausea", "causes pancreatitis", "causes gallbladder disease",
            "causes kidney injury"]
    assert list(result.ranked) == kept  # extraction's order, not the ranking's
    assert result.extraction is not None and result.extraction.target_claims == kept
    assert list(result.candidates) == kept
    assert result.thin_claims == []
