"""Stage 2 — search the scholarly APIs for every claim and cache the results.

For each claim:
  1. Build the queries (``retrieval/query_builder.py``).
  2. Run every query against every provider in parallel.
  3. Drop trial protocols and retracted papers.
Then, across all claims:
  4. Merge duplicate papers (``retrieval/dedup.py``).
  5. Fill in the PMID of any paper that arrived with only a DOI, when Europe
     PMC knows one (``retrieval/identifiers.py``), and merge again.
  6. Ask PubMed whether any paper a non-PubMed provider supplied has been
     retracted, and drop it if so.
  7. Save them to the cache and embed new abstracts in chunks
     (``retrieval/cache.py``).

A search that two claims share — the same terms, the same pass — runs once
and both claims get its answer. Claims often reduce to identical queries
("increases muscle power" and "improves muscle mass" both keep only the
muscle hint), and each duplicate spent NCBI requests and OpenAlex credits for
an answer already in hand.

Three outcomes for a claim, deliberately kept apart:

* **Searched, found nothing.** Fine. The article can honestly say "no evidence".
* **Every provider call failed.** The stage fails (retryable). Otherwise a
  throttled search would reach synthesis as the same empty list as a real
  "nothing found", and publish a confident verdict nobody checked.
* **The query names no substance.** The stage fails (not retryable). An
  outcome-only query returns real but off-topic papers, and every later
  stage would read that as success.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from app.domain.contracts import CachedCandidate, CandidatePaper, ExtractionOutput
from app.domain.enums import SourceApi
from app.evidence.grading import is_protocol, is_retracted
from app.llm.embeddings.base import EmbeddingError
from app.pipeline.stages import PipelineContext, StageError, StageName
from app.retrieval.base import ProviderError, RateLimited, ScholarlyProvider, SearchQuery
from app.retrieval.cache import CachedSource, SourceCache
from app.retrieval.dedup import merge_candidates, unique_papers
from app.retrieval.identifiers import DoiRegistry, PmidResolver
from app.retrieval.query_builder import QueryStrategy, UnanchoredQuery
from app.retrieval.retractions import PaperIdentity, RetractionSource

logger = logging.getLogger(__name__)

ComposeQueries = Callable[[str, str, list[str]], list[SearchQuery]]

#: One provider's answer to one search, shared by every claim that asks it.
_SearchKey = tuple[SourceApi, str, bool, int, int | None]


@dataclass
class _SearchStats:
    """Counters collected while searching, reported as stage metrics."""

    provider_hits: Counter[SourceApi] = field(default_factory=Counter)
    failures: int = 0
    rate_limited: int = 0
    protocols_dropped: int = 0
    retractions_dropped: int = 0
    searches_shared: int = 0


class RetrieveStage:
    name = StageName.RETRIEVE

    def __init__(
        self,
        providers: list[ScholarlyProvider],
        strategy: QueryStrategy,
        cache: SourceCache,
        max_candidates_per_claim: int = 50,
        resolver: PmidResolver | None = None,
        retraction_screen: RetractionSource | None = None,
        doi_registry: DoiRegistry | None = None,
    ) -> None:
        self._providers = providers
        self._strategy = strategy
        self._cache = cache
        self._max_candidates = max_candidates_per_claim
        self._resolver = resolver
        self._retraction_screen = retraction_screen
        self._doi_registry = doi_registry

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.extraction is None:
            raise StageError(self.name, "extraction stage did not run")

        # A refinement pass re-searches only the claims RANK found thin, with a
        # broader query (the outcome dropped, the substance kept).
        refining = ctx.refine_round > 0
        claims = list(ctx.thin_claims) if refining else list(ctx.extraction.target_claims)
        compose = self._strategy.broaden if refining else self._strategy.build

        # Steps 1–3, per claim.
        stats = _SearchStats()
        found: dict[str, list[CandidatePaper]] = {}
        unsearched: list[str] = []
        searches: dict[_SearchKey, asyncio.Future[list[CandidatePaper]]] = {}
        for claim in claims:
            queries = self._build_queries(compose, claim, ctx.extraction)
            found[claim], all_failed = await self._search(claim, queries, stats, searches)
            if all_failed:
                unsearched.append(claim)

        if unsearched:
            detail = f"; {stats.rate_limited} rate limited" if stats.rate_limited else ""
            raise StageError(
                self.name,
                f"{len(unsearched)} of {len(found)} claims went unsearched "
                f"(every provider call failed{detail}): "
                f"{', '.join(repr(claim) for claim in unsearched)}",
                retryable=True,  # usually a throttle that has since cleared
            )

        # Step 4. Across all claims at once, so a paper answering two claims
        # is the same merged record in both lists.
        merged = merge_candidates(found)

        # Step 5. Before the cap, so a paper that turns out to be a duplicate
        # frees its slot for the next one.
        merged, pmid_metrics = await self._fill_pmids(merged)
        # Also before the cap, for the same reason: a refused paper frees its slot.
        merged, screen_metrics = await self._screen_retractions(merged)
        merged, doi_metrics = await self._screen_unregistered(merged, ctx.candidates)
        candidates = {claim: papers[: self._max_candidates] for claim, papers in merged.items()}
        for claim, papers in candidates.items():
            logger.info(
                "claim %r: %d raw -> %d deduped candidates",
                claim,
                len(found[claim]),
                len(papers),
            )

        # Step 6.
        unique = unique_papers(candidates)
        try:
            cached = await self._cache.upsert_many(unique)
            await self._cache.ensure_embeddings(cached)
        except EmbeddingError as exc:
            # The embedding server being down is an outage, not a property of
            # the topic: the next attempt finds the cache rows already written.
            raise StageError(self.name, f"embedding failed: {exc}", retryable=True) from exc
        paired = self._pair_with_ids(candidates, cached)

        # A refinement pass keeps every claim the first pass already answered.
        if refining:
            paired = {**ctx.candidates, **paired}

        raw_total = sum(len(papers) for papers in found.values())
        ctx.record_metrics(
            self.name,
            {
                "provider_hits": dict(stats.provider_hits),
                "candidates_total": raw_total,
                "protocols_dropped": stats.protocols_dropped,
                "retractions_dropped": stats.retractions_dropped,
                "candidates_unique": len(unique),
                # Near zero with several providers on means dedup stopped working.
                "duplicates_merged": raw_total - sum(len(p) for p in candidates.values()),
                "provider_failures": stats.failures,
                # Separate from failures: throttling wants slower pacing,
                # failure wants a look at the provider.
                "rate_limited": stats.rate_limited,
                "cache_hits": sum(1 for entry in cached if entry.had_embedding),
                "refine_round": ctx.refine_round,
                "claims_searched": len(claims),
                "searches_shared": stats.searches_shared,
                **pmid_metrics,
                **screen_metrics,
                **doi_metrics,
            },
        )
        return ctx.model_copy(update={"candidates": paired})

    async def _fill_pmids(
        self, merged: dict[str, list[CandidatePaper]]
    ) -> tuple[dict[str, list[CandidatePaper]], dict[str, object]]:
        """Step 5. Give a DOI-only paper its PMID, when one exists.

        Without it the paper is invisible to the full-text lookup and to the
        retraction sweep, and a PubMed-only record of the same paper stays a
        second candidate that the model would cite as independent support. So
        the merge runs again once PMIDs are in: a DOI-only and a PMID-only
        record of one paper only unify when something carries both.

        Never fails the stage. A lookup that could not run leaves every paper
        exactly as its provider reported it, which is how retrieval worked
        before this step existed; ``pmid_lookup_failed`` records it.
        """
        dois = sorted(
            {
                paper.doi.strip().lower()
                for papers in merged.values()
                for paper in papers
                if paper.doi and not paper.pmid
            }
        )
        if self._resolver is None or not dois:
            return merged, {"dois_without_pmid": len(dois), "pmids_filled": 0}

        try:
            found = await self._resolver.pmids_for_dois(dois)
        except ProviderError as exc:
            logger.warning("PMID lookup for %d DOI-only papers failed: %s", len(dois), exc)
            return merged, {
                "dois_without_pmid": len(dois),
                "pmids_filled": 0,
                "pmid_lookup_failed": str(exc),
            }

        def filled(paper: CandidatePaper) -> CandidatePaper:
            if paper.pmid or not paper.doi:
                return paper
            pmid = found.get(paper.doi.strip().lower())
            return paper.model_copy(update={"pmid": pmid}) if pmid else paper

        if found:
            logger.info("filled PMIDs for %d of %d DOI-only papers", len(found), len(dois))
            merged = merge_candidates(
                {claim: [filled(paper) for paper in papers] for claim, papers in merged.items()}
            )
        return merged, {"dois_without_pmid": len(dois), "pmids_filled": len(found)}

    async def _screen_retractions(
        self, merged: dict[str, list[CandidatePaper]]
    ) -> tuple[dict[str, list[CandidatePaper]], dict[str, object]]:
        """Step 6. Drop papers PubMed knows are retracted but their provider did not say.

        Step 3 refuses a retraction only when the record carries publication
        types, and only PubMed's do: an OpenAlex or Europe PMC record of a
        retracted trial arrives typed ``article`` and sails through. Measured
        2026-10-01, an apple-cider-vinegar article cited exactly such a trial
        (PMID 38966098) with a ``supported`` verdict. Any paper that has a PMID
        but whose record did not come from PubMed is therefore looked up — one
        ``esummary`` request per 200 papers.

        Fails open, like the rest of this stage's lookups: a screen that could
        not run leaves the papers as they were, which is how retrieval worked
        before it existed, and ``retraction_screen_failed`` says so. The
        post-publication sweep (``scripts/check_retractions.py``) still covers
        what this misses, including DOI-only papers.
        """
        if self._retraction_screen is None:
            return merged, {}
        by_key = {
            paper.dedup_key: paper
            for papers in merged.values()
            for paper in papers
            if paper.pmid and paper.source_api is not SourceApi.PUBMED
        }
        if not by_key:
            return merged, {"retraction_screen_checked": 0, "retractions_dropped_late": 0}
        try:
            verdicts = await self._retraction_screen.check(
                [PaperIdentity(source_id=key, pmid=paper.pmid) for key, paper in by_key.items()]
            )
        except Exception as exc:
            logger.warning("retraction screen failed for %d papers: %s", len(by_key), exc)
            return merged, {
                "retraction_screen_checked": 0,
                "retractions_dropped_late": 0,
                "retraction_screen_failed": str(exc),
            }
        retracted = {key for key, verdict in verdicts.items() if verdict.retracted}
        for key in retracted:
            paper = by_key[key]
            logger.info(
                "refused retracted paper pmid=%s (via %s): %r",
                paper.pmid,
                paper.source_api,
                paper.title,
            )
        screened = {
            claim: [paper for paper in papers if paper.dedup_key not in retracted]
            for claim, papers in merged.items()
        }
        return screened, {
            "retraction_screen_checked": sum(1 for v in verdicts.values() if v.checked),
            "retractions_dropped_late": len(retracted),
        }

    async def _screen_unregistered(
        self,
        merged: dict[str, list[CandidatePaper]],
        screened: dict[str, list[CachedCandidate]],
    ) -> tuple[dict[str, list[CandidatePaper]], dict[str, object]]:
        """Step 7. Drop a DOI-only paper whose DOI doi.org has never heard of.

        Fails open: a DOI doi.org could not answer for is kept (``DoiRegistry``).
        ``screened`` is an earlier round's candidates, which already passed.
        """
        if self._doi_registry is None:
            return merged, {}
        passed = {
            c.paper.doi.strip().lower() for cs in screened.values() for c in cs if c.paper.doi
        }
        dois = {
            paper.doi.strip().lower()
            for papers in merged.values()
            for paper in papers
            if paper.doi and not paper.pmid
        } - passed
        gone = await self._doi_registry.unregistered(dois) if dois else set()
        for doi in sorted(gone):
            logger.info("refused paper with unregistered doi=%s", doi)
        screened = {
            claim: [p for p in papers if not (p.doi and p.doi.strip().lower() in gone)]
            for claim, papers in merged.items()
        }
        return screened, {"dois_checked": len(dois), "unregistered_dois_dropped": len(gone)}

    def _build_queries(
        self, compose: ComposeQueries, claim: str, extraction: ExtractionOutput
    ) -> list[SearchQuery]:
        """Step 1. A query that names no substance fails the stage for good."""
        try:
            return compose(claim, extraction.product, extraction.ingredients)
        except UnanchoredQuery as exc:
            # Not retryable: the same extraction would build the same query.
            raise StageError(self.name, str(exc), retryable=False) from exc

    async def _search(
        self,
        claim: str,
        queries: list[SearchQuery],
        stats: _SearchStats,
        searches: dict[_SearchKey, asyncio.Future[list[CandidatePaper]]],
    ) -> tuple[list[CandidatePaper], bool]:
        """Steps 2–3: every query against every provider, in parallel.

        Returns the usable papers, and whether every call failed. Some calls
        failing is tolerated; it only lowers recall. A search another claim
        already made is not made again: its answer — or its failure, which
        counts against this claim too — is shared through ``searches``.
        """
        calls = [(provider, query) for query in queries for provider in self._providers]
        pending: list[asyncio.Future[list[CandidatePaper]]] = []
        for provider, query in calls:
            key = (
                provider.source_api,
                query.terms,
                query.reviews_only,
                query.max_results,
                query.min_year,
            )
            if key in searches:
                stats.searches_shared += 1
            else:
                searches[key] = asyncio.ensure_future(provider.search(query))
            pending.append(searches[key])
        results = await asyncio.gather(*pending, return_exceptions=True)

        papers: list[CandidatePaper] = []
        failed = 0
        for (provider, _), result in zip(calls, results, strict=True):
            if isinstance(result, BaseException):
                failed += 1
                if isinstance(result, RateLimited):
                    stats.rate_limited += 1
                logger.warning(
                    "provider %s failed for claim %r: %s", provider.source_api, claim, result
                )
                continue
            usable = self._screen(result, stats)
            papers.extend(usable)
            stats.provider_hits[provider.source_api] += len(usable)

        stats.failures += failed
        return papers, bool(calls) and failed == len(calls)

    @staticmethod
    def _screen(papers: list[CandidatePaper], stats: _SearchStats) -> list[CandidatePaper]:
        """Step 3. Drop trial protocols and retracted papers before they are cached.

        A protocol is a plan and reports no findings; a retracted paper reports
        findings the literature has withdrawn. Retractions issued *after* a
        paper was cached are caught by ``scripts/check_retractions.py``.
        """
        kept: list[CandidatePaper] = []
        for paper in papers:
            if is_protocol(paper.raw_study_type, paper.title):
                stats.protocols_dropped += 1
            elif is_retracted(paper.raw_study_type):
                stats.retractions_dropped += 1
                logger.info(
                    "refused retracted paper pmid=%s doi=%s: %r",
                    paper.pmid,
                    paper.doi,
                    paper.title,
                )
            else:
                kept.append(paper)
        return kept

    def _pair_with_ids(
        self, candidates: dict[str, list[CandidatePaper]], cached: list[CachedSource]
    ) -> dict[str, list[CachedCandidate]]:
        """Attach each candidate's ``sources`` row id, which RankStage needs.

        A missing id raises rather than dropping the paper: a paper lost here
        would silently read as thinner evidence downstream.
        """
        source_ids = {entry.paper.dedup_key: entry.source_id for entry in cached}
        try:
            return {
                claim: [
                    CachedCandidate(source_id=source_ids[paper.dedup_key], paper=paper)
                    for paper in papers
                ]
                for claim, papers in candidates.items()
            }
        except KeyError as exc:
            raise StageError(
                self.name, f"source cache returned no row for candidate {exc}"
            ) from exc
