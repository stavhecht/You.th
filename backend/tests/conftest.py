"""Shared fixtures.

The validation tests need an ``AsyncSession`` only so ``check_sources_resolve``
can look up rows. Rather than require a live Postgres for what is otherwise
pure logic, ``FakeSession`` serves ``select(Source)`` from an in-memory list.

The DB-backed invariants (the CHECK constraint, the immutability trigger) are
in ``test_db_invariants.py`` and *do* need a real database — those are exactly
the guarantees a fake cannot verify, since the whole point is that Postgres
enforces them independently of application code.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("JWT_SECRET", "test-secret-that-is-at-least-32-characters-long")
# Every TestClient(create_app()) runs the lifespan, which starts the trend-scan
# scheduler, and it ticks at once: with the ledger's last scan over a day old,
# each API test started a real PubMed scan writing to the dev database. The
# scheduler's own tests pass `discovery_scan_enabled` explicitly.
os.environ.setdefault("DISCOVERY_SCAN_ENABLED", "false")

from app.domain.contracts import (
    ArticleBody,
    ArticleSection,
    CitationGroup,
    PromptSource,
    SynthesisInput,
    SynthesisOutput,
)
from app.domain.enums import StudyType, Verdict


class _FakeScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any | None:
        return self._rows[0] if self._rows else None


class FakeSource:
    """Stands in for a ``sources`` row."""

    def __init__(
        self,
        id: str,
        pmid: str | None = "12345678",
        doi: str | None = "10.1000/example",
        study_type: StudyType = StudyType.RCT,
    ) -> None:
        self.id = id
        self.pmid = pmid
        self.doi = doi
        self.study_type = study_type


class FakeSession:
    """Serves ``select(Source).where(Source.id.in_(...))`` from memory."""

    def __init__(self, sources: list[FakeSource]) -> None:
        self._by_id = {source.id: source for source in sources}

    async def execute(self, statement: Any) -> _FakeResult:
        wanted = self._extract_ids(statement)
        if wanted is None:
            return _FakeResult(list(self._by_id.values()))
        return _FakeResult([self._by_id[i] for i in wanted if i in self._by_id])

    @staticmethod
    def _extract_ids(statement: Any) -> list[str] | None:
        """Pull the id list out of the compiled ``IN`` clause."""
        try:
            params = statement.compile().params
        except Exception:
            return None
        for value in params.values():
            if isinstance(value, (list, tuple)):
                return list(value)
        return None


class _NullSavepoint:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: Any) -> bool:
        return False


class FakeMediaSession:
    """Accepts ``store_image``'s insert without a database.

    ``services/media.py`` writes image bytes through the session, so the stages
    and services that store pictures now need one. This keeps the imagery tests
    DB-free, which they were and should stay: what they pin is the prompt's
    content rules and the promise that a decoration cannot fail a run, and
    neither is a fact about Postgres.

    ``inserted`` counts the writes, so a test can still tell "stored two
    frames" from "stored none" — the property the old tests got by counting
    files in a tmp_path.
    """

    def __init__(self) -> None:
        self.inserted = 0

    def begin_nested(self) -> _NullSavepoint:
        # The real SAVEPOINT exists to keep an IntegrityError from poisoning
        # the long-lived pipeline session. There is no transaction to protect
        # here, so this is a no-op that satisfies the `async with`.
        return _NullSavepoint()

    async def execute(self, _statement: Any) -> None:
        self.inserted += 1


#: The claim the shared ``payload`` fixture is about.
DEFAULT_CLAIM = "reduces stress"


def make_prompt_source(
    handle: str,
    source_id: str,
    *,
    study_type: StudyType = StudyType.RCT,
    claims: list[str] | None = None,
) -> PromptSource:
    """A source in the prompt, attributed to ``claims``.

    ``claims`` defaults to the single claim the ``payload`` fixture uses, since
    most tests here care about handles rather than evidence quorum. Tests that
    do care pass it explicitly — attribution is what the per-claim quorum
    counts against.
    """
    return PromptSource(
        handle=handle,
        title=f"Study {handle}",
        abstract="An abstract long enough to be plausible for testing purposes.",
        journal="Journal of Testing",
        year=2022,
        study_type=study_type,
        source_id=source_id,
        claims=claims if claims is not None else [DEFAULT_CLAIM],
    )


@pytest.fixture
def payload() -> SynthesisInput:
    """A prompt payload offering exactly S1 and S2, both backing the claim.

    Two sources at a supported-tier grade, so this payload clears the evidence
    quorum — tests asserting a *failure* of some other check are not also
    fighting invariant #3.
    """
    return SynthesisInput(
        product="Ashwagandha KSM-66",
        target_claims=[DEFAULT_CLAIM],
        sources=[
            make_prompt_source("S1", "src-1"),
            make_prompt_source("S2", "src-2"),
        ],
    )


def make_draft(
    *,
    verdict: Verdict = Verdict.MIXED,
    beat_2: str = "One randomised trial reported lower evening cortisol [S1].",
    sections: list[ArticleSection] | None = None,
    citations: list[CitationGroup] | None = None,
) -> SynthesisOutput:
    return SynthesisOutput(
        headline="Ashwagandha and stress: what the evidence shows",
        verdict=verdict,
        summary="A small number of trials suggest a modest effect.",
        body=ArticleBody(
            beat_1_claim="The product claims to reduce day-to-day stress.",
            beat_2_evidence=beat_2,
            sections=sections or [],
            beat_3_bottom_line="The evidence is early and the trials are small.",
        ),
        citations=citations
        if citations is not None
        else [CitationGroup(claim="reduces cortisol", source_ids=["S1"])],
    )
