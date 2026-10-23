"""Does each cited paper exist, is it the paper the article says it is, and does it still stand?

The pipeline's own validation (``evidence/validation.py``) proves a citation
points at a ``sources`` row that carries a PMID or DOI. It cannot prove the
identifier is real: it trusts whatever the provider returned. This checks the
identifiers against the registries themselves.

**Reused, not re-written:** existence and retraction status come from
``retrieval/retractions.py::RetractionChecker`` with its PubMed and Crossref
sources — the same code the retraction sweep runs, with its three-state
verdict, so "the registry could not be reached" is never scored as "the paper
does not exist". What is added here:

* format checks on the identifiers (a malformed PMID or DOI is caught before
  any request);
* a **title match** against the registry's record, which catches an
  identifier attached to the wrong paper — a real PMID can still be a
  misattribution;
* a DOI-only existence check through doi.org's handle API, for DOIs Crossref
  does not register (DataCite and others);
* duplicates among the cited sources (dedup failing upstream);
* optionally, a live GET of each source URL.

Requests go through ``EvalHttp`` with a recorder of their own, so they are
cached like everything else and never counted as the system's tool calls. The
PubMed and Crossref requests are byte-identical to the retraction checker's,
so the title lookup hits the recording the checker just made.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from app.config import Settings
from app.retrieval.retractions import (
    CROSSREF_BASE,
    EUTILS_BASE,
    PUBMED_BATCH_SIZE,
    CrossrefRetractionSource,
    PaperIdentity,
    PubMedRetractionSource,
    RetractionChecker,
)
from app.retrieval.throttle import HttpClient, RateLimiter, ThrottledClient

PMID_RE = re.compile(r"^\d{1,9}$")
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
#: A registry title at least this similar to ours is the same paper.
TITLE_MATCH = 0.85


@dataclass
class SourceCheck:
    source_id: str
    pmid: str | None
    doi: str | None
    pmid_format_ok: bool | None = None
    doi_format_ok: bool | None = None
    exists: bool | None = None
    registry_title: str | None = None
    title_similarity: float | None = None
    retracted: bool | None = None
    concern: bool | None = None
    url_ok: bool | None = None
    duplicate_of: str | None = None
    injected: bool = False
    #: verified | nonexistent | title_mismatch | retracted | malformed | unverifiable
    status: str = "unverifiable"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def _normalise_title(title: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", title)).lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def title_similarity(left: str, right: str) -> float:
    a, b = _normalise_title(left), _normalise_title(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


class SourceValidator:
    def __init__(self, http: HttpClient, settings: Settings, *, check_urls: bool) -> None:
        pubmed_rps = 7.0 if settings.pubmed_api_key else 2.0
        self._pubmed = ThrottledClient(http, RateLimiter(pubmed_rps), provider="pubmed")
        self._crossref = ThrottledClient(http, RateLimiter(5.0), provider="crossref")
        self._doi = ThrottledClient(http, RateLimiter(5.0), provider="doi_registry")
        self._raw = http
        self._api_key = settings.pubmed_api_key or None
        self._mailto = settings.openalex_mailto or None
        self._check_urls = check_urls

    async def validate(self, sources: list[dict[str, Any]]) -> dict[str, SourceCheck]:
        """``sources``: paper records (``harness/trace.py::PaperRecord`` dumps)."""
        checks = {
            source["source_id"]: SourceCheck(
                source_id=source["source_id"],
                pmid=source.get("pmid"),
                doi=source.get("doi"),
                injected=bool(source.get("injected")),
            )
            for source in sources
        }
        by_id = {source["source_id"]: source for source in sources}

        for check in checks.values():
            if check.pmid:
                check.pmid_format_ok = bool(PMID_RE.match(check.pmid.strip()))
            if check.doi:
                check.doi_format_ok = bool(DOI_RE.match(check.doi.strip()))

        identities = [
            PaperIdentity(
                source_id=c.source_id,
                pmid=c.pmid if c.pmid_format_ok else None,
                doi=c.doi if c.doi_format_ok else None,
            )
            for c in checks.values()
            if c.pmid_format_ok or c.doi_format_ok
        ]
        if identities:
            checker = RetractionChecker(
                [
                    PubMedRetractionSource(self._pubmed, self._api_key),
                    CrossrefRetractionSource(self._crossref, self._mailto),
                ]
            )
            verdicts = await checker.check(identities)
            for source_id, verdict in verdicts.items():
                check = checks[source_id]
                if verdict.checked:
                    check.exists = True
                    check.retracted = verdict.retracted
                    check.concern = verdict.concern
                    if verdict.detail:
                        check.notes.append(verdict.detail)

        titles = await self._registry_titles(list(checks.values()))
        for source_id, title in titles.items():
            check = checks[source_id]
            check.registry_title = title
            check.title_similarity = round(
                title_similarity(title, by_id[source_id]["title"]), 3
            )

        for check in checks.values():
            if check.exists is None and check.doi_format_ok:
                check.exists = await self._doi_registered(check.doi or "")
            if check.exists is None and check.pmid_format_ok:
                # The PubMed answer was "no such record" (an error entry):
                # RetractionChecker leaves that unchecked by design, so it is
                # re-read here as the existence answer it also is.
                check.exists = await self._pmid_known(check.pmid or "")
            if self._check_urls and by_id[check.source_id].get("url"):
                check.url_ok = await self._url_resolves(by_id[check.source_id]["url"])

        self._mark_duplicates(list(checks.values()), by_id)
        for check in checks.values():
            check.status = _status(check)
        return checks

    async def verify_identifiers(self, keys: list[str]) -> dict[str, bool | None]:
        """``pmid:<id>`` / ``doi:<doi>`` -> exists? Used to vet gold ground truth:
        an identifier no registry confirms is dropped rather than scored against."""
        found: dict[str, bool | None] = {}
        for key in keys:
            kind, _, value = key.partition(":")
            if kind == "pmid" and PMID_RE.match(value):
                found[key] = await self._pmid_known(value)
            elif kind == "doi" and DOI_RE.match(value):
                found[key] = await self._doi_registered(value)
            else:
                found[key] = False
        return found

    async def _registry_titles(self, checks: list[SourceCheck]) -> dict[str, str]:
        titles: dict[str, str] = {}
        with_pmid = [c for c in checks if c.pmid_format_ok]
        for start in range(0, len(with_pmid), PUBMED_BATCH_SIZE):
            batch = with_pmid[start : start + PUBMED_BATCH_SIZE]
            params: dict[str, str] = {
                "db": "pubmed",
                "id": ",".join(str(c.pmid) for c in batch),
                "retmode": "json",
            }
            if self._api_key:
                params["api_key"] = self._api_key
            try:
                response = await self._pubmed.get(f"{EUTILS_BASE}/esummary.fcgi", params=params)
                result = response.json().get("result", {})
            except Exception:
                continue
            for check in batch:
                record = result.get(str(check.pmid)) or {}
                if record.get("title") and not record.get("error"):
                    titles[check.source_id] = str(record["title"])
        for check in checks:
            if check.source_id in titles or not check.doi_format_ok:
                continue
            params_cr = {"mailto": self._mailto} if self._mailto else None
            try:
                response = await self._crossref.get(
                    f"{CROSSREF_BASE}/{check.doi}", params=params_cr
                )
                if response.status_code != 200:
                    continue
                found = (response.json().get("message") or {}).get("title") or []
            except Exception:
                continue
            if found:
                titles[check.source_id] = str(found[0])
        return titles

    async def _doi_registered(self, doi: str) -> bool | None:
        try:
            response = await self._doi.get(f"https://doi.org/api/handles/{doi}")
        except Exception:
            return None
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        return None

    async def _pmid_known(self, pmid: str) -> bool | None:
        params: dict[str, str] = {"db": "pubmed", "id": pmid, "retmode": "json"}
        if self._api_key:
            params["api_key"] = self._api_key
        try:
            response = await self._pubmed.get(f"{EUTILS_BASE}/esummary.fcgi", params=params)
            record = response.json().get("result", {}).get(pmid)
        except Exception:
            return None
        if record is None:
            return None
        return not bool(record.get("error"))

    async def _url_resolves(self, url: str) -> bool | None:
        try:
            response = await self._raw.get(url)
        except Exception:
            return None
        return response.status_code < 400

    @staticmethod
    def _mark_duplicates(checks: list[SourceCheck], by_id: dict[str, dict[str, Any]]) -> None:
        seen: dict[str, str] = {}
        titles: list[tuple[str, str]] = []
        for check in checks:
            keys = [
                f"pmid:{check.pmid}" if check.pmid else "",
                f"doi:{(check.doi or '').lower()}",
            ]
            for key in filter(lambda k: k and not k.endswith(":"), keys):
                if key in seen and seen[key] != check.source_id:
                    check.duplicate_of = seen[key]
                seen.setdefault(key, check.source_id)
            title = by_id[check.source_id].get("title") or ""
            for other_id, other_title in titles:
                if check.duplicate_of is None and title_similarity(title, other_title) >= 0.95:
                    check.duplicate_of = other_id
            titles.append((check.source_id, title))


def _status(check: SourceCheck) -> str:
    # Malformed only when no identifier is usable: a bad DOI beside a good
    # PMID still leaves a paper that can be looked up.
    malformed = check.pmid_format_ok is False or check.doi_format_ok is False
    if malformed and not (check.pmid_format_ok or check.doi_format_ok):
        return "malformed"
    if check.retracted:
        return "retracted"
    if check.exists is False:
        return "nonexistent"
    if check.exists is None:
        return "unverifiable"
    if check.title_similarity is not None and check.title_similarity < TITLE_MATCH:
        return "title_mismatch"
    return "verified"
