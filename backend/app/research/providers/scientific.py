"""Literature sizing over the pipeline's own PubMed and Europe PMC clients.

No new HTTP client: these adapt ``retrieval/pubmed.py::PubMedProvider`` and
``retrieval/providers.py::EuropePMCProvider``, which gained a ``count`` method
for this, so throttling, retries and the NCBI throttle detector are the ones
the pipeline already relies on.

Counts, not papers, and four of them per question: everything, the recent
part, reviews and meta-analyses, and randomised trials. That is what
``scoring.evidence_status`` needs to tell "3,000 papers, no trials" from
"40 papers, 6 trials and 2 meta-analyses".
"""

from __future__ import annotations

import re

from app.research.contracts import EvidenceCounts
from app.research.providers.base import EvidenceQuery, ProviderUnavailable
from app.retrieval.base import ProviderError
from app.retrieval.providers import EuropePMCProvider, europe_pmc_query
from app.retrieval.pubmed import REVIEW_FILTER, PubMedProvider

_RCT_FILTER = '"randomized controlled trial"[pt]'
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]*", re.IGNORECASE)


def pubmed_terms(query: EvidenceQuery) -> str:
    """``"vitamin d"[tiab] AND (sleep[tiab] AND quality[tiab])``.

    The subject is a phrase — "vitamin d" split into words searches for any
    paper with a "d" in it. The outcome's words are ANDed rather than phrased,
    so "muscle recovery" also finds "recovery of muscle function".
    """
    subject = _phrase(query.subject)
    words = _words(query.outcome)
    if not words:
        return f"{subject}[tiab]"
    outcome = " AND ".join(f"{word}[tiab]" for word in words)
    return f"{subject}[tiab] AND ({outcome})"


def europe_pmc_terms(query: EvidenceQuery) -> str:
    subject = _phrase(query.subject)
    words = _words(query.outcome)
    return f"{subject} AND ({' AND '.join(words)})" if words else subject


class PubMedEvidenceProvider:
    name = "pubmed"

    def __init__(self, pubmed: PubMedProvider) -> None:
        self._pubmed = pubmed

    async def profile(self, query: EvidenceQuery) -> EvidenceCounts:
        return await self.profile_terms(pubmed_terms(query), query.recent_since.isoformat())

    async def profile_terms(self, terms: str, recent_since: str) -> EvidenceCounts:
        """Counts for ready-made PubMed terms, e.g. ``TemplateQueryStrategy``'s."""
        since = recent_since.replace("-", "/")
        try:
            total = await self._pubmed.count(terms)
            recent = await self._pubmed.count(terms, min_date=since) if total else 0
            reviews = await self._pubmed.count(f"({terms}) AND {REVIEW_FILTER}") if total else 0
            rcts = await self._pubmed.count(f"({terms}) AND {_RCT_FILTER}") if total else 0
        except ProviderError as exc:
            raise ProviderUnavailable(f"pubmed count failed: {exc}") from exc
        return EvidenceCounts(
            source=self.name,
            query=terms,
            total=total,
            recent=recent,
            reviews_or_meta=reviews,
            rcts=rcts,
        )


class EuropePMCEvidenceProvider:
    """Europe PMC's view of the same question.

    Its publication-type tags are coarser than PubMed's: ``review`` includes
    narrative reviews, so ``reviews_or_meta`` here over-counts. It is the
    cross-check and the fallback, not the primary.
    """

    name = "europe_pmc"

    def __init__(self, europe_pmc: EuropePMCProvider) -> None:
        self._epmc = europe_pmc

    async def profile(self, query: EvidenceQuery) -> EvidenceCounts:
        return await self._profile(europe_pmc_terms(query), query.recent_since.isoformat())

    async def profile_terms(self, terms: str, recent_since: str) -> EvidenceCounts:
        """Counts for the pipeline's PubMed terms, in titles and abstracts: unscoped,
        Europe PMC searches full text and over-counts ~40x (``EuropePMCProvider.search``)."""
        return await self._profile(f"TITLE_ABS:({europe_pmc_query(terms)})", recent_since)

    async def _profile(self, terms: str, since: str) -> EvidenceCounts:
        try:
            total = await self._epmc.count(terms)
            recent = (
                await self._epmc.count(f"({terms}) AND FIRST_PDATE:[{since} TO 3000-12-31]")
                if total
                else 0
            )
            reviews = (
                await self._epmc.count(
                    f'({terms}) AND (PUB_TYPE:"review" OR PUB_TYPE:"meta-analysis" '
                    'OR PUB_TYPE:"systematic-review")'
                )
                if total
                else 0
            )
            rcts = (
                await self._epmc.count(f'({terms}) AND PUB_TYPE:"randomized controlled trial"')
                if total
                else 0
            )
        except ProviderError as exc:
            raise ProviderUnavailable(f"europe pmc count failed: {exc}") from exc
        return EvidenceCounts(
            source=self.name,
            query=terms,
            total=total,
            recent=recent,
            reviews_or_meta=reviews,
            rcts=rcts,
        )


def _phrase(text: str) -> str:
    cleaned = " ".join(_words(text))
    if not cleaned:
        raise ValueError(f"no searchable words in subject {text!r}")
    return f'"{cleaned}"'


def _words(text: str) -> list[str]:
    return [word.lower() for word in _WORD_RE.findall(text or "")]
