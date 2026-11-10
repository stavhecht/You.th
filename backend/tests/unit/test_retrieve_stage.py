"""``RetrieveStage`` failure handling — the boundary around "no evidence".

Every test here is about one distinction: a claim that was searched and found
nothing, versus a claim that was never successfully searched. They arrive at
the next stage as the same empty list, and since ``SynthesizeStage`` turns an
empty list into a finished ``no_evidence`` article rather than crashing, the
second one publishes a confident verdict about literature nobody read.
"""

from __future__ import annotations

import pytest

from app.domain.contracts import CandidatePaper, ExtractionOutput
from app.domain.enums import SourceApi
from app.pipeline.stages import PipelineContext, StageError, StageName
from app.pipeline.steps.retrieve import RetrieveStage
from app.retrieval.base import ProviderError, RateLimited, SearchQuery
from app.retrieval.cache import CachedSource
from app.retrieval.query_builder import UnanchoredQuery


def _paper(pmid: str) -> CandidatePaper:
    return CandidatePaper(
        pmid=pmid,
        title=f"Paper {pmid}",
        abstract="An abstract long enough to be worth embedding and reading.",
        source_api=SourceApi.PUBMED,
    )


class _Provider:
    """Answers per claim: a list of papers, or an exception to raise."""

    def __init__(
        self,
        source_api: SourceApi,
        answers: dict[str, list[CandidatePaper] | Exception],
    ) -> None:
        self._source_api = source_api
        self._answers = answers
        self.searches = 0
        #: Every query this provider was handed, so a test can assert which
        #: claims a pass searched and with what terms.
        self.queries: list[SearchQuery] = []

    @property
    def source_api(self) -> SourceApi:
        return self._source_api

    async def search(self, query: SearchQuery) -> list[CandidatePaper]:
        self.searches += 1
        self.queries.append(query)
        answer = self._answers.get(query.claim, [])
        if isinstance(answer, Exception):
            raise answer
        return answer


class _OneQueryStrategy:
    """One query per claim, so failure counts map onto claims directly."""

    def build(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        return [SearchQuery(claim=claim, terms=claim)]

    def broaden(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        return [SearchQuery(claim=claim, terms=f"{claim} broadened")]


class _NullCache:
    """Assigns a deterministic id per paper instead of touching a database.

    It must return one entry per paper: RetrieveStage now pairs every candidate
    with its row id before leaving the stage, and a cache that answered with
    less would be dropping sources between retrieval and ranking.
    """

    async def upsert_many(self, papers: list[CandidatePaper]) -> list[CachedSource]:
        return [
            CachedSource(source_id=f"src-{paper.dedup_key}", paper=paper, had_embedding=False)
            for paper in papers
        ]

    async def ensure_embeddings(self, cached: list[CachedSource]) -> None:
        return None


def _stage(*providers: _Provider) -> RetrieveStage:
    return RetrieveStage(
        list(providers),  # type: ignore[arg-type]
        _OneQueryStrategy(),
        _NullCache(),  # type: ignore[arg-type]
    )


def _ctx(*claims: str) -> PipelineContext:
    return PipelineContext(
        run_id="run-1",
        topic="ashwagandha",
        extraction=ExtractionOutput(
            product="Ashwagandha capsules",
            target_claims=list(claims),
            ingredients=["ashwagandha"],
        ),
    )


# ---------------------------------------------------------------------------
# Searched and found nothing — the publishable empty result.
# ---------------------------------------------------------------------------


async def test_a_claim_searched_with_no_results_is_not_an_error() -> None:
    """The fringe-trend case. Raising here would turn the most important thing
    this product can say into a crash — see ``services/no_evidence.py``."""
    stage = _stage(_Provider(SourceApi.PUBMED, {"reduces stress": []}))

    result = await stage.run(_ctx("reduces stress"))

    assert result.candidates == {"reduces stress": []}


async def test_a_claim_that_lost_one_provider_of_two_still_proceeds() -> None:
    """Degraded recall is an acceptable outcome; it yields a more cautious
    verdict, which is the correct response to having read less."""
    stage = _stage(
        _Provider(SourceApi.PUBMED, {"reduces stress": ProviderError("down")}),
        _Provider(SourceApi.EUROPE_PMC, {"reduces stress": [_paper("1")]}),
    )

    result = await stage.run(_ctx("reduces stress"))

    assert [c.paper.pmid for c in result.candidates["reduces stress"]] == ["1"]
    assert result.metrics[StageName.RETRIEVE]["provider_failures"] == 1


async def test_a_surviving_provider_returning_nothing_still_counts_as_searched() -> None:
    """One provider down and the other genuinely empty is a real empty result.

    This is the case the coverage check must *not* catch, and the reason it
    counts successful calls rather than candidates.
    """
    stage = _stage(
        _Provider(SourceApi.PUBMED, {"reduces stress": RateLimited("429")}),
        _Provider(SourceApi.EUROPE_PMC, {"reduces stress": []}),
    )

    result = await stage.run(_ctx("reduces stress"))

    assert result.candidates == {"reduces stress": []}


# ---------------------------------------------------------------------------
# Never searched — not publishable.
# ---------------------------------------------------------------------------


async def test_a_claim_whose_every_provider_failed_fails_the_stage() -> None:
    stage = _stage(
        _Provider(SourceApi.PUBMED, {"reduces stress": RateLimited("429")}),
        _Provider(SourceApi.EUROPE_PMC, {"reduces stress": ProviderError("down")}),
    )

    with pytest.raises(StageError) as exc_info:
        await stage.run(_ctx("reduces stress"))

    assert "went unsearched" in str(exc_info.value)
    assert "'reduces stress'" in str(exc_info.value)


async def test_the_failure_is_retryable() -> None:
    """The usual cause is a throttle that has since cleared. The alternative to
    retrying is publishing a guess."""
    stage = _stage(_Provider(SourceApi.PUBMED, {"c": RateLimited("429")}))

    with pytest.raises(StageError) as exc_info:
        await stage.run(_ctx("c"))

    assert exc_info.value.retryable is True
    assert exc_info.value.stage is StageName.RETRIEVE


async def test_one_unsearched_claim_among_several_still_fails_the_stage() -> None:
    """Counted per claim, not per run.

    Two claims searching cleanly does not license a verdict on the third. The
    previous check only fired when *every* call in the whole run failed, so
    this run proceeded and the article covered a claim nobody looked up.
    """
    stage = _stage(
        _Provider(
            SourceApi.PUBMED,
            {
                "reduces stress": [_paper("1")],
                "improves sleep": [_paper("2")],
                "boosts immunity": RateLimited("429"),
            },
        )
    )

    with pytest.raises(StageError) as exc_info:
        await stage.run(_ctx("reduces stress", "improves sleep", "boosts immunity"))

    message = str(exc_info.value)
    assert "1 of 3 claims went unsearched" in message
    assert "'boosts immunity'" in message
    assert "rate limited" in message


async def test_rate_limits_are_reported_separately_from_other_failures() -> None:
    """The remedy differs: throttling wants slower pacing, failure wants a look
    at the provider. One counter cannot tell you which."""
    stage = _stage(
        _Provider(SourceApi.PUBMED, {"c": RateLimited("429")}),
        _Provider(SourceApi.EUROPE_PMC, {"c": ProviderError("boom")}),
        _Provider(SourceApi.OPENALEX, {"c": [_paper("1")]}),
    )

    result = await stage.run(_ctx("c"))

    metrics = result.metrics[StageName.RETRIEVE]
    assert (metrics["provider_failures"], metrics["rate_limited"]) == (2, 1)


# ---------------------------------------------------------------------------
# Searched cleanly, asked the wrong question — the third category.
# ---------------------------------------------------------------------------


class _UnanchoredStrategy:
    """Stands in for an extraction that named no substance."""

    def build(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        raise UnanchoredQuery(claim, product, ingredients)


async def test_a_query_that_names_no_substance_fails_the_stage() -> None:
    """The most dangerous of the three outcomes, because every downstream
    signal reads as success: papers come back, ranking scores them, synthesis
    cites them, and validation resolves every handle. Only this stage can still
    see that the query never mentioned the product."""
    stage = RetrieveStage(
        [_Provider(SourceApi.PUBMED, {"reduces stress": [_paper("1")]})],  # type: ignore[list-item]
        _UnanchoredStrategy(),
        _NullCache(),  # type: ignore[arg-type]
    )

    with pytest.raises(StageError, match="no searchable substance"):
        await stage.run(_ctx("reduces stress"))


async def test_the_unanchored_failure_is_not_retryable() -> None:
    """Unlike the throttle case, nothing clears between attempts: the subject
    terms come from the extraction, so all three attempts would compose the
    identical unusable query. The fix is upstream."""
    stage = RetrieveStage(
        [_Provider(SourceApi.PUBMED, {"reduces stress": []})],  # type: ignore[list-item]
        _UnanchoredStrategy(),
        _NullCache(),  # type: ignore[arg-type]
    )

    with pytest.raises(StageError) as exc_info:
        await stage.run(_ctx("reduces stress"))

    assert exc_info.value.retryable is False


async def test_no_provider_is_called_when_the_query_cannot_be_anchored() -> None:
    """Failing before the fan-out, not after: an off-topic search still costs
    the provider budget, and a 429 earned on a query nobody wanted delays the
    next run that does."""
    provider = _Provider(SourceApi.PUBMED, {"reduces stress": [_paper("1")]})
    stage = RetrieveStage(
        [provider],  # type: ignore[list-item]
        _UnanchoredStrategy(),
        _NullCache(),  # type: ignore[arg-type]
    )

    with pytest.raises(StageError):
        await stage.run(_ctx("reduces stress"))

    assert provider.searches == 0


# ---------------------------------------------------------------------------
# The handoff to RankStage.
# ---------------------------------------------------------------------------


async def test_every_candidate_leaves_paired_with_its_source_id() -> None:
    """RankStage used to re-upsert the whole candidate set to recover these.

    Retrieval is the only stage that learns the row ids, so dropping them here
    forced the next stage to ask the database again — around a hundred extra
    statements, most of them single-row UPDATEs against rows written seconds
    earlier.
    """
    stage = _stage(_Provider(SourceApi.PUBMED, {"reduces stress": [_paper("1"), _paper("2")]}))

    candidates = (await stage.run(_ctx("reduces stress"))).candidates["reduces stress"]

    assert [c.source_id for c in candidates] == ["src-pmid:1", "src-pmid:2"]
    assert [c.paper.pmid for c in candidates] == ["1", "2"]


async def test_a_paper_answering_two_claims_carries_one_id_in_both() -> None:
    """The pairing must not reintroduce the identity split that ``dedup.py``
    exists to prevent: one paper under two ids is two sources to everything
    downstream, and the model cites them as independent findings."""
    shared = _paper("7")
    stage = _stage(
        _Provider(SourceApi.PUBMED, {"reduces stress": [shared], "improves sleep": [shared]})
    )

    result = await stage.run(_ctx("reduces stress", "improves sleep"))

    assert (
        result.candidates["reduces stress"][0].source_id
        == result.candidates["improves sleep"][0].source_id
    )


# ---------------------------------------------------------------------------
# Trial protocols never enter the candidate set.
# ---------------------------------------------------------------------------


def _protocol(pmid: str) -> CandidatePaper:
    """A protocol as PubMed actually returns one: the tag says it is a plan,
    the abstract reads exactly like the trial it has not yet run."""
    return CandidatePaper(
        pmid=pmid,
        title=f"Creatine and recovery: study protocol for a randomised trial {pmid}",
        abstract=(
            "This randomized, double-blind, placebo-controlled trial will enrol "
            "120 adults to assess creatine on workout performance."
        ),
        raw_study_type="Clinical Trial Protocol; Journal Article",
        source_api=SourceApi.PUBMED,
    )


async def test_a_trial_protocol_never_becomes_a_candidate() -> None:
    """Dropped at retrieval rather than graded down.

    A protocol reports no findings, so there is no verdict it could honestly
    support — admitting it can only cost a top-k slot and put trial vocabulary
    in front of a model asked what the evidence says. Three reached the cache
    and scored ``rct`` before this existed, whose ceiling is ``supported``.
    """
    stage = _stage(
        _Provider(SourceApi.PUBMED, {"reduces stress": [_protocol("9"), _paper("1")]})
    )

    result = await stage.run(_ctx("reduces stress"))

    assert [c.paper.pmid for c in result.candidates["reduces stress"]] == ["1"]


async def test_dropped_protocols_are_counted_in_the_metrics() -> None:
    """A number that climbs means the queries are drifting toward planned work
    rather than reported results — invisible in the article, which simply has
    fewer sources."""
    stage = _stage(
        _Provider(
            SourceApi.PUBMED,
            {"reduces stress": [_protocol("9"), _protocol("8"), _paper("1")]},
        )
    )

    metrics = (await stage.run(_ctx("reduces stress"))).metrics[StageName.RETRIEVE]

    assert metrics["protocols_dropped"] == 2
    assert metrics["provider_hits"] == {SourceApi.PUBMED: 1}


async def test_a_claim_answered_only_by_protocols_is_empty_not_unsearched() -> None:
    """The distinction ``unsearched`` exists to protect. The search ran and the
    provider answered; everything it returned was unusable. That is a claim
    with no evidence, which must reach synthesis, not a retryable failure.
    """
    stage = _stage(_Provider(SourceApi.PUBMED, {"reduces stress": [_protocol("9")]}))

    result = await stage.run(_ctx("reduces stress"))

    assert result.candidates["reduces stress"] == []


# ---------------------------------------------------------------------------
# Retracted papers never enter the candidate set either.
# ---------------------------------------------------------------------------


def _retracted(pmid: str) -> CandidatePaper:
    """A retracted paper as PubMed returns one.

    Note the abstract still reads like a finding — that is the point. Nothing
    in the text says the paper was withdrawn, so prose heuristics cannot catch
    this and the publication type is the only signal.
    """
    return CandidatePaper(
        pmid=pmid,
        title=f"Creatine supplementation improves sprint performance {pmid}",
        abstract=(
            "In this randomized controlled trial of 60 adults, creatine "
            "supplementation improved repeated sprint performance."
        ),
        raw_study_type="Journal Article; Randomized Controlled Trial; Retracted Publication",
        source_api=SourceApi.PUBMED,
    )


async def test_a_retracted_paper_never_becomes_a_candidate() -> None:
    """Refused, not merely graded down.

    ``retracted publication`` was already in NEGATIVE_PUBLICATION_TYPES, which
    capped such a paper at ``unknown`` and therefore capped the verdict it
    could support at ``weak``. That was never sufficient: a grade cap governs
    how confident the article may *sound* and does nothing about the citation,
    so the article still said "one trial found X [S3]" pointing at a study the
    journal had withdrawn.
    """
    stage = _stage(
        _Provider(SourceApi.PUBMED, {"reduces stress": [_retracted("9"), _paper("1")]})
    )

    result = await stage.run(_ctx("reduces stress"))

    assert [c.paper.pmid for c in result.candidates["reduces stress"]] == ["1"]


async def test_a_retraction_notice_is_refused_too() -> None:
    """The notice announcing a retraction has no findings of its own.

    Different reason from the retracted paper above, same outcome: one is a
    finding that no longer stands, the other is an announcement. A model handed
    either would cite it as evidence.
    """
    notice = CandidatePaper(
        pmid="7",
        title="Retraction: Creatine supplementation improves sprint performance",
        abstract="This article has been retracted at the request of the editors.",
        raw_study_type="Retraction of Publication",
        source_api=SourceApi.PUBMED,
    )
    stage = _stage(_Provider(SourceApi.PUBMED, {"reduces stress": [notice, _paper("1")]}))

    result = await stage.run(_ctx("reduces stress"))

    assert [c.paper.pmid for c in result.candidates["reduces stress"]] == ["1"]


async def test_dropped_retractions_are_counted_separately_from_protocols() -> None:
    """Two counters, because they answer different questions.

    Protocols climbing means the queries are drifting toward planned work.
    Retractions climbing means a topic's literature is in trouble, which is
    worth knowing about the *subject* rather than about the query.
    """
    stage = _stage(
        _Provider(
            SourceApi.PUBMED,
            {"reduces stress": [_retracted("9"), _protocol("8"), _paper("1")]},
        )
    )

    metrics = (await stage.run(_ctx("reduces stress"))).metrics[StageName.RETRIEVE]

    assert metrics["retractions_dropped"] == 1
    assert metrics["protocols_dropped"] == 1
    assert metrics["provider_hits"] == {SourceApi.PUBMED: 1}


async def test_a_paper_with_no_publication_types_is_not_assumed_retracted() -> None:
    """OpenAlex supplies no publication types at all.

    ``is_retracted`` deliberately reads no prose — there is no reliable textual
    tell, and guessing from a title containing "retracted" would catch the
    notices while missing every retracted paper. So a provider with no metadata
    contributes nothing here, and the paper is admitted. Closing that gap is
    the sweep's job, not this filter's.
    """
    bare = CandidatePaper(
        pmid="5",
        title="Retracted trials in sports nutrition: a bibliometric analysis",
        abstract="We surveyed retracted randomized trials in sports nutrition.",
        raw_study_type=None,
        source_api=SourceApi.OPENALEX,
    )
    stage = _stage(_Provider(SourceApi.PUBMED, {"reduces stress": [bare]}))

    result = await stage.run(_ctx("reduces stress"))

    assert [c.paper.pmid for c in result.candidates["reduces stress"]] == ["5"]


# ---------------------------------------------------------------------------
# The refinement pass — the second half of the RETRIEVE <-> RANK cycle.
# ---------------------------------------------------------------------------


def _answering(*claims: str) -> _Provider:
    return _Provider(
        SourceApi.PUBMED,
        {claim: [_paper(str(index + 1))] for index, claim in enumerate(claims)},
    )


async def test_the_first_pass_searches_every_claim() -> None:
    """``refine_round`` 0 is the ordinary path and must be untouched by any of
    this: the loop is an addition, not a change to what a normal run does."""
    provider = _answering("claim a", "claim b")

    result = await _stage(provider).run(_ctx("claim a", "claim b"))

    assert sorted(result.candidates) == ["claim a", "claim b"]
    assert result.metrics["retrieve"]["refine_round"] == 0
    assert result.metrics["retrieve"]["claims_searched"] == 2


async def test_a_refinement_pass_searches_only_the_thin_claims() -> None:
    """Re-querying claims that are already well served spends provider budget
    against a per-IP ceiling the worker shares, to re-answer a settled question.
    """
    provider = _answering("claim a", "claim b")
    ctx = _ctx("claim a", "claim b").model_copy(
        update={"refine_round": 1, "thin_claims": ["claim b"]}
    )

    result = await _stage(provider).run(ctx)

    assert [query.claim for query in provider.queries] == ["claim b"]
    assert result.metrics["retrieve"]["claims_searched"] == 1
    assert result.metrics["retrieve"]["refine_round"] == 1


async def test_a_refinement_pass_uses_the_broadened_query() -> None:
    provider = _answering("claim a")
    ctx = _ctx("claim a").model_copy(update={"refine_round": 1, "thin_claims": ["claim a"]})

    await _stage(provider).run(ctx)

    assert provider.queries[0].terms == "claim a broadened"


async def test_a_refinement_pass_keeps_the_claims_it_did_not_re_search() -> None:
    """Returning only the re-searched claims would drop the rest of the
    article's evidence, which reads downstream as a thinner corpus and so as a
    more cautious verdict: the exact failure the loop exists to remove."""
    first = await _stage(_answering("claim a", "claim b")).run(_ctx("claim a", "claim b"))

    refined = await _stage(_answering("claim a", "claim b")).run(
        first.model_copy(update={"refine_round": 1, "thin_claims": ["claim b"]})
    )

    assert sorted(refined.candidates) == ["claim a", "claim b"]
    assert refined.candidates["claim a"] == first.candidates["claim a"]


# ---------------------------------------------------------------------------
# A DOI-only paper is given its PMID when one exists.
# ---------------------------------------------------------------------------


def _doi_only(doi: str) -> CandidatePaper:
    return CandidatePaper(
        doi=doi,
        title=f"Paper {doi}",
        abstract="An abstract long enough to be worth embedding and reading.",
        source_api=SourceApi.OPENALEX,
    )


class _Resolver:
    def __init__(self, answer: dict[str, str] | Exception) -> None:
        self._answer = answer
        self.asked: list[str] = []

    async def pmids_for_dois(self, dois: list[str]) -> dict[str, str]:
        self.asked = dois
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def _resolving_stage(provider: _Provider, resolver: _Resolver) -> RetrieveStage:
    return RetrieveStage(
        [provider],  # type: ignore[list-item]
        _OneQueryStrategy(),
        _NullCache(),  # type: ignore[arg-type]
        resolver=resolver,
    )


async def test_a_doi_only_paper_is_given_its_pmid() -> None:
    resolver = _Resolver({"10.1/abc": "111"})
    provider = _Provider(SourceApi.OPENALEX, {"reduces stress": [_doi_only("10.1/ABC")]})

    result = await _resolving_stage(provider, resolver).run(_ctx("reduces stress"))

    (candidate,) = result.candidates["reduces stress"]
    assert (candidate.paper.pmid, candidate.paper.doi) == ("111", "10.1/ABC")
    assert resolver.asked == ["10.1/abc"]
    assert result.metrics[StageName.RETRIEVE]["pmids_filled"] == 1


async def test_a_filled_pmid_merges_the_paper_with_its_pubmed_record() -> None:
    """The DOI-only and the PMID-only record of one paper only unify once
    something carries both; left apart, the model cites one study twice."""
    resolver = _Resolver({"10.1/abc": "111"})
    pubmed = _Provider(SourceApi.PUBMED, {"reduces stress": [_paper("111")]})
    openalex = _Provider(SourceApi.OPENALEX, {"reduces stress": [_doi_only("10.1/abc")]})
    stage = RetrieveStage(
        [pubmed, openalex],  # type: ignore[list-item]
        _OneQueryStrategy(),
        _NullCache(),  # type: ignore[arg-type]
        resolver=resolver,
    )

    result = await stage.run(_ctx("reduces stress"))

    (candidate,) = result.candidates["reduces stress"]
    assert (candidate.paper.pmid, candidate.paper.doi) == ("111", "10.1/abc")


async def test_a_doi_with_no_pmid_is_left_as_it_was() -> None:
    provider = _Provider(SourceApi.OPENALEX, {"reduces stress": [_doi_only("10.1/abc")]})

    result = await _resolving_stage(provider, _Resolver({})).run(_ctx("reduces stress"))

    (candidate,) = result.candidates["reduces stress"]
    assert candidate.paper.pmid is None
    assert result.metrics[StageName.RETRIEVE]["pmids_filled"] == 0


async def test_a_failed_pmid_lookup_never_fails_the_stage() -> None:
    """The papers were found; only an extra identifier is missing. Failing here
    would throw away a search that worked."""
    resolver = _Resolver(ProviderError("europe_pmc returned 503"))
    provider = _Provider(SourceApi.OPENALEX, {"reduces stress": [_doi_only("10.1/abc")]})

    result = await _resolving_stage(provider, resolver).run(_ctx("reduces stress"))

    (candidate,) = result.candidates["reduces stress"]
    assert candidate.paper.pmid is None
    assert "503" in result.metrics[StageName.RETRIEVE]["pmid_lookup_failed"]


async def test_papers_that_already_have_a_pmid_are_not_looked_up() -> None:
    resolver = _Resolver({})
    provider = _Provider(SourceApi.PUBMED, {"reduces stress": [_paper("111")]})

    await _resolving_stage(provider, resolver).run(_ctx("reduces stress"))

    assert resolver.asked == []


# ---------------------------------------------------------------------------
# Shared searches, the retraction screen, and embedding outages.
# ---------------------------------------------------------------------------


class _SameTermsStrategy:
    """Every claim reduces to the same terms — "muscle power" and "muscle mass"
    both keeping only the muscle hint."""

    def build(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        return [SearchQuery(claim=claim, terms="(Creatine) AND muscle")]

    def broaden(self, claim: str, product: str, ingredients: list[str]) -> list[SearchQuery]:
        return self.build(claim, product, ingredients)


async def test_a_search_two_claims_share_is_made_once() -> None:
    provider = _Provider(SourceApi.PUBMED, {"increases muscle power": [_paper("1")]})
    stage = RetrieveStage([provider], _SameTermsStrategy(), _NullCache())  # type: ignore[list-item,arg-type]

    ctx = await stage.run(_ctx("increases muscle power", "improves muscle mass"))

    assert provider.searches == 1
    assert ctx.metrics["retrieve"]["searches_shared"] == 1
    assert set(ctx.candidates) == {"increases muscle power", "improves muscle mass"}


class _Screen:
    name = "pubmed"

    def __init__(self, retracted: set[str] | None = None, fail: bool = False) -> None:
        self._retracted = retracted or set()
        self._fail = fail
        self.asked: list[str] = []

    async def check(self, papers):  # type: ignore[no-untyped-def]
        from app.retrieval.retractions import RetractionVerdict

        if self._fail:
            raise RuntimeError("esummary unreachable")
        self.asked = [paper.pmid for paper in papers]
        return {
            paper.source_id: RetractionVerdict(
                checked=True, retracted=paper.pmid in self._retracted
            )
            for paper in papers
        }


def _openalex_paper(pmid: str) -> CandidatePaper:
    return CandidatePaper(
        pmid=pmid,
        title=f"Paper {pmid}",
        abstract="An abstract long enough to be worth embedding and reading.",
        raw_study_type="article",
        source_api=SourceApi.OPENALEX,
    )


async def test_a_retracted_paper_a_tagless_provider_supplied_is_dropped() -> None:
    """An OpenAlex record of a retracted trial is typed "article"; only PubMed
    says it was retracted. One such trial was cited with a supported verdict."""
    papers = [_openalex_paper("38966098"), _openalex_paper("2")]
    provider = _Provider(SourceApi.OPENALEX, {"reduces stress": papers})
    screen = _Screen(retracted={"38966098"})
    stage = RetrieveStage(
        [provider],
        _OneQueryStrategy(),
        _NullCache(),
        retraction_screen=screen,  # type: ignore[list-item,arg-type]
    )

    ctx = await stage.run(_ctx("reduces stress"))

    kept = [c.paper.pmid for c in ctx.candidates["reduces stress"]]
    assert kept == ["2"]
    assert sorted(screen.asked) == ["2", "38966098"]
    assert ctx.metrics["retrieve"]["retractions_dropped_late"] == 1


async def test_pubmed_records_are_not_looked_up_again() -> None:
    """PubMed's own record already carried its publication types to step 3."""
    provider = _Provider(SourceApi.PUBMED, {"reduces stress": [_paper("1")]})
    screen = _Screen()
    stage = RetrieveStage(
        [provider],
        _OneQueryStrategy(),
        _NullCache(),
        retraction_screen=screen,  # type: ignore[list-item,arg-type]
    )
    await stage.run(_ctx("reduces stress"))
    assert screen.asked == []


async def test_a_screen_that_cannot_run_leaves_the_papers_and_says_so() -> None:
    provider = _Provider(SourceApi.OPENALEX, {"reduces stress": [_openalex_paper("2")]})
    stage = RetrieveStage(
        [provider],
        _OneQueryStrategy(),
        _NullCache(),
        retraction_screen=_Screen(fail=True),  # type: ignore[list-item,arg-type]
    )
    ctx = await stage.run(_ctx("reduces stress"))
    assert [c.paper.pmid for c in ctx.candidates["reduces stress"]] == ["2"]
    assert "retraction_screen_failed" in ctx.metrics["retrieve"]


async def test_an_embedding_outage_is_retryable() -> None:
    from app.llm.embeddings.base import EmbeddingError

    class _Down(_NullCache):
        async def ensure_embeddings(self, cached: list[CachedSource]) -> None:
            raise EmbeddingError("ollama unreachable")

    provider = _Provider(SourceApi.PUBMED, {"reduces stress": [_paper("1")]})
    stage = RetrieveStage([provider], _OneQueryStrategy(), _Down())  # type: ignore[list-item,arg-type]
    with pytest.raises(StageError) as exc_info:
        await stage.run(_ctx("reduces stress"))
    assert exc_info.value.retryable is True


class _Registry:
    def __init__(self, gone: set[str]) -> None:
        self._gone = gone
        self.asked: set[str] = set()

    async def unregistered(self, dois: set[str]) -> set[str]:
        self.asked = set(dois)
        return self._gone & set(dois)


async def test_a_doi_only_paper_whose_doi_was_never_registered_is_dropped() -> None:
    """2026-10-23: an OpenAlex "systematic review" cited with a DOI doi.org 404s."""
    registry = _Registry({"10.53555/fake"})
    provider = _Provider(
        SourceApi.OPENALEX,
        {"reduces stress": [_doi_only("10.53555/FAKE"), _doi_only("10.1/real"), _paper("7")]},
    )
    stage = RetrieveStage(
        [provider],  # type: ignore[list-item]
        _OneQueryStrategy(),
        _NullCache(),  # type: ignore[arg-type]
        doi_registry=registry,  # type: ignore[arg-type]
    )

    result = await stage.run(_ctx("reduces stress"))

    kept = {c.paper.doi or c.paper.pmid for c in result.candidates["reduces stress"]}
    assert kept == {"10.1/real", "7"}
    assert registry.asked == {"10.53555/fake", "10.1/real"}, "only DOI-only papers are asked"
    assert result.metrics[StageName.RETRIEVE]["unregistered_dois_dropped"] == 1


async def test_doi_org_is_asked_and_an_unanswered_doi_is_kept() -> None:
    import httpx

    from app.retrieval.identifiers import DoiRegistry

    class Http:
        async def get(self, url: str, **_: object) -> httpx.Response:
            if url.endswith("/10.1/down"):
                raise httpx.ConnectError("down")
            return httpx.Response(404 if url.endswith("/10.1/gone") else 200)

    gone = await DoiRegistry(Http()).unregistered(["10.1/gone", "10.1/real", "10.1/down"])  # type: ignore[arg-type]

    assert gone == {"10.1/gone"}


async def test_a_refinement_round_does_not_ask_about_a_doi_already_screened() -> None:
    from app.domain.contracts import CachedCandidate

    registry = _Registry(set())
    stage = RetrieveStage(
        [],
        _OneQueryStrategy(),
        _NullCache(),  # type: ignore[arg-type]
        doi_registry=registry,  # type: ignore[arg-type]
    )
    earlier = {"c": [CachedCandidate(source_id="s1", paper=_doi_only("10.1/Seen"))]}
    merged = {"c": [_doi_only("10.1/seen"), _doi_only("10.1/new")]}

    _, metrics = await stage._screen_unregistered(merged, earlier)

    assert registry.asked == {"10.1/new"}
    assert metrics["dois_checked"] == 1
