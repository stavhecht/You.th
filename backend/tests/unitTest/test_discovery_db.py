"""Schema guarantees for trend discovery, enforced by Postgres.

Three of these cannot be expressed anywhere but in the database, and each backs
a claim the rest of the feature is built on:

* ``discovery_candidates_one_live_per_substance`` is the whole answer to "don't
  propose the same substance every fortnight". A trend that emerges keeps
  clearing the bar, so without the partial unique index each scan appends
  another row and the desk fills with one proposal at three different scores.
* ``discovery_observations``' composite primary key is what makes re-reading an
  overlapping window free — which is what lets the scan tolerate MeSH's
  indexing lag at all, and what makes running the script twice a no-op.
* The three CHECK constraints are the same house rule as
  ``articles_published_requires_reviewer``: a decision someone is answerable for
  cannot be recorded without the human attached.

Needs a live database, and skips silently without one — see the note in
``test_db_invariants.py``. **A skipped run verifies none of the above.**

    docker compose up -d db
    python -m scripts.migrate
    pytest tests/unitTest/test_discovery_db.py
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.domain.enums import (
    DiscoveryCandidateStatus,
    DiscoveryDescriptorKind,
    RunOrigin,
    RunStatus,
    StudyType,
    UserRole,
)
from app.domain.models import User
from app.security.auth import hash_password

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url


@pytest.fixture
async def session() -> Any:
    """A session whose writes are rolled back after each test.

    Per-test engine for the same reason as ``test_db_invariants.py``: asyncpg
    connections bind to the loop that created them.
    """
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(sa.text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip(f"no database at {DATABASE_URL}")

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        try:
            yield db
        finally:
            await db.rollback()
    await engine.dispose()


async def _make_scan(db: AsyncSession) -> str:
    scan_id = str(uuid.uuid4())
    await db.execute(
        sa.text(
            "INSERT INTO discovery_scans (id, window_start, window_end)"
            " VALUES (:id, :start, :end)"
        ),
        {"id": scan_id, "start": date(2026, 8, 1), "end": date(2026, 9, 1)},
    )
    return scan_id


#: Synthetic descriptor UIs. Valid under
#: ``discovery_descriptors_ui_is_mesh`` but outside the range MeSH actually
#: assigns, so these can never collide with rows a real scan wrote. That matters
#: because this suite runs against a working database: after a bootstrap the
#: observations table holds tens of thousands of rows, and a test asserting
#: "there are five" against real creatine would fail for a reason that has
#: nothing to do with the guarantee it is checking.
_FAKE_UIS = ("D999901", "D999902", "D999903")


def _fake_pmid() -> str:
    """A PMID no record has. Same reason as ``_FAKE_UIS``."""
    return f"t{uuid.uuid4().hex[:12]}"


async def _make_descriptor(
    db: AsyncSession,
    ui: str,
    *,
    kind: DiscoveryDescriptorKind = DiscoveryDescriptorKind.SUBSTANCE,
) -> str:
    await db.execute(
        sa.text(
            "INSERT INTO discovery_descriptors (ui, name, kind)"
            " VALUES (:ui, :name, :kind) ON CONFLICT (ui) DO NOTHING"
        ),
        {"ui": ui, "name": f"Descriptor {ui}", "kind": kind.value},
    )
    return ui


async def _make_user(db: AsyncSession) -> str:
    user_id = str(uuid.uuid4())
    await db.execute(
        sa.text(
            "INSERT INTO users (id, email, password_hash, display_name, role, created_at)"
            " VALUES (:id, :email, :hash, :name, :role, now())"
        ),
        {
            "id": user_id,
            "email": f"reviewer-{user_id[:8]}@example.com",
            "hash": hash_password("a-sufficiently-long-password"),
            "name": "Test Reviewer",
            "role": UserRole.REVIEWER.value,
        },
    )
    return user_id


async def _make_run(db: AsyncSession) -> str:
    run_id = str(uuid.uuid4())
    await db.execute(
        sa.text(
            "INSERT INTO pipeline_runs (id, topic, status) VALUES (:id, :topic, :status)"
        ),
        {"id": run_id, "topic": "creatine for cognition", "status": RunStatus.QUEUED.value},
    )
    return run_id


_INSERT_CANDIDATE = sa.text(
    "INSERT INTO discovery_candidates"
    " (id, scan_id, substance_ui, topic, score, paper_count, baseline_count,"
    "  rationale, status, pipeline_run_id, decided_by, decided_at, dismiss_reason)"
    " VALUES (:id, :scan_id, :substance_ui, :topic, :score, :paper_count,"
    "  :baseline_count, :rationale, :status, :run_id, :decided_by, :decided_at,"
    "  :dismiss_reason)"
)


def _candidate_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "topic": "creatine for cognition",
        "score": 4.2,
        "paper_count": 17,
        "baseline_count": 3.5,
        "rationale": "{}",
        "status": DiscoveryCandidateStatus.PROPOSED.value,
        "run_id": None,
        "decided_by": None,
        "decided_at": None,
        "dismiss_reason": None,
    }
    params.update(overrides)
    return params


# --- the anti-repeat guarantee ---------------------------------------------


async def test_one_live_proposal_per_substance(session: AsyncSession) -> None:
    """A second live proposal for the same substance is refused by the index.

    This is what a re-detected trend hits every fortnight. The scan's real write
    is ``ON CONFLICT … DO UPDATE``, which refreshes the row instead — but the
    guarantee has to live in the database, or a second write path reintroduces
    the duplicate the console then shows twice.
    """
    scan_id = await _make_scan(session)
    ui = await _make_descriptor(session, _FAKE_UIS[0])

    await session.execute(
        _INSERT_CANDIDATE, _candidate_params(scan_id=scan_id, substance_ui=ui)
    )
    await session.flush()

    with pytest.raises(IntegrityError):
        await session.execute(
            _INSERT_CANDIDATE, _candidate_params(scan_id=scan_id, substance_ui=ui)
        )
        await session.flush()


async def test_decided_candidates_do_not_block_a_new_proposal(
    session: AsyncSession,
) -> None:
    """History is unconstrained — only the *live* proposal is unique.

    A substance may have been dismissed in March, promoted in June and be live
    again now. All three rows have to coexist, because that history is exactly
    what ``suppression_reason`` reads to decide whether to propose it again.
    """
    scan_id = await _make_scan(session)
    ui = await _make_descriptor(session, _FAKE_UIS[0])
    reviewer = await _make_user(session)
    run_id = await _make_run(session)
    decided = datetime.now(UTC) - timedelta(days=90)

    await session.execute(
        _INSERT_CANDIDATE,
        _candidate_params(
            scan_id=scan_id,
            substance_ui=ui,
            status=DiscoveryCandidateStatus.DISMISSED.value,
            decided_by=reviewer,
            decided_at=decided,
            dismiss_reason="too thin",
        ),
    )
    await session.execute(
        _INSERT_CANDIDATE,
        _candidate_params(
            scan_id=scan_id,
            substance_ui=ui,
            status=DiscoveryCandidateStatus.PROMOTED.value,
            run_id=run_id,
            decided_by=reviewer,
            decided_at=decided,
        ),
    )
    await session.execute(
        _INSERT_CANDIDATE, _candidate_params(scan_id=scan_id, substance_ui=ui)
    )
    await session.flush()

    count = await session.scalar(
        sa.text("SELECT count(*) FROM discovery_candidates WHERE substance_ui = :ui"),
        {"ui": ui},
    )
    assert count == 3


# --- the ledger's idempotence ----------------------------------------------


async def test_reobserving_a_window_changes_nothing(session: AsyncSession) -> None:
    """The overlap is free, which is the premise the window design rests on.

    Every scan deliberately re-reads weeks a previous scan already counted,
    because MeSH indexing lags PubMed entry. With incremented counters that is
    double counting; with this table it is a no-op.
    """
    scan_id = await _make_scan(session)
    ui = await _make_descriptor(session, _FAKE_UIS[0])
    rows = [
        {"ui": ui, "pmid": _fake_pmid(), "date": date(2026, 8, 20), "scan": scan_id}
        for _ in range(5)
    ]
    statement = sa.text(
        "INSERT INTO discovery_observations"
        " (descriptor_ui, pmid, entrez_date, is_substance)"
        " VALUES (:ui, :pmid, :date, true)"
        " ON CONFLICT (descriptor_ui, pmid) DO NOTHING"
    )

    await session.execute(statement, rows)
    await session.execute(statement, rows)
    await session.flush()

    count = await session.scalar(
        sa.text("SELECT count(*) FROM discovery_observations WHERE descriptor_ui = :ui"),
        {"ui": ui},
    )
    assert count == 5


async def test_observation_survives_its_scan(session: AsyncSession) -> None:
    """Deleting a scan must not rewrite the baseline it contributed to.

    An observation is evidence about the literature, not about the scan that
    noticed it, and a hole in the baseline reads as a surge the next time that
    descriptor appears. Since 2026-09-30 the ledger carries no link to its scan
    at all, which makes this true by construction; the test keeps it true.
    """
    scan_id = await _make_scan(session)
    ui = await _make_descriptor(session, _FAKE_UIS[0])
    pmid = _fake_pmid()
    await session.execute(
        sa.text(
            "INSERT INTO discovery_observations"
            " (descriptor_ui, pmid, entrez_date, is_substance)"
            " VALUES (:ui, :pmid, :date, true)"
        ),
        {"ui": ui, "pmid": pmid, "date": date(2026, 8, 20), "scan": scan_id},
    )
    await session.flush()

    await session.execute(
        sa.text("DELETE FROM discovery_scans WHERE id = :id"), {"id": scan_id}
    )
    await session.flush()

    row = (
        await session.execute(
            sa.text(
                "SELECT pmid FROM discovery_observations WHERE descriptor_ui = :ui"
            ),
            {"ui": ui},
        )
    ).one()
    assert row.pmid == pmid


# --- the decision constraints ----------------------------------------------


async def test_promoted_candidate_requires_a_run(session: AsyncSession) -> None:
    scan_id = await _make_scan(session)
    ui = await _make_descriptor(session, _FAKE_UIS[0])
    reviewer = await _make_user(session)

    with pytest.raises(IntegrityError):
        await session.execute(
            _INSERT_CANDIDATE,
            _candidate_params(
                scan_id=scan_id,
                substance_ui=ui,
                status=DiscoveryCandidateStatus.PROMOTED.value,
                decided_by=reviewer,
                decided_at=datetime.now(UTC),
            ),
        )
        await session.flush()


async def test_decided_candidate_requires_a_reviewer(session: AsyncSession) -> None:
    scan_id = await _make_scan(session)
    ui = await _make_descriptor(session, _FAKE_UIS[0])

    with pytest.raises(IntegrityError):
        await session.execute(
            _INSERT_CANDIDATE,
            _candidate_params(
                scan_id=scan_id,
                substance_ui=ui,
                status=DiscoveryCandidateStatus.DISMISSED.value,
                dismiss_reason="too thin",
            ),
        )
        await session.flush()


async def test_dismissed_candidate_requires_a_reason(session: AsyncSession) -> None:
    scan_id = await _make_scan(session)
    ui = await _make_descriptor(session, _FAKE_UIS[0])
    reviewer = await _make_user(session)

    with pytest.raises(IntegrityError):
        await session.execute(
            _INSERT_CANDIDATE,
            _candidate_params(
                scan_id=scan_id,
                substance_ui=ui,
                status=DiscoveryCandidateStatus.DISMISSED.value,
                decided_by=reviewer,
                decided_at=datetime.now(UTC),
            ),
        )
        await session.flush()


# --- the column added to an existing table ---------------------------------


async def test_run_origin_defaults_to_console(session: AsyncSession) -> None:
    """0005 cannot break ``create_run``, which does not set ``origin``.

    The column is a convenience for cost accounting; if it were ever required at
    insert time it would turn the console's own enqueue into a 500.
    """
    run_id = await _make_run(session)
    await session.flush()

    origin = await session.scalar(
        sa.text("SELECT origin FROM pipeline_runs WHERE id = :id"), {"id": run_id}
    )
    assert origin == RunOrigin.CONSOLE.value


async def test_observation_study_type_uses_the_shared_enum(session: AsyncSession) -> None:
    """The ledger grades papers with the pipeline's own vocabulary.

    A second study-type enum for discovery would drift from ``evidence/grading``
    the first time a grade is added, and the quality weight would silently start
    scoring an unknown value as zero.
    """
    scan_id = await _make_scan(session)
    ui = await _make_descriptor(session, _FAKE_UIS[0])
    pmid = _fake_pmid()
    await session.execute(
        sa.text(
            "INSERT INTO discovery_observations"
            " (descriptor_ui, pmid, entrez_date, is_substance, study_type)"
            " VALUES (:ui, :pmid, :date, true, :grade)"
        ),
        {
            "ui": ui,
            "pmid": pmid,
            "date": date(2026, 8, 20),
            "grade": StudyType.SYSTEMATIC_REVIEW.value,
            "scan": scan_id,
        },
    )
    await session.flush()

    grade = await session.scalar(
        sa.text("SELECT study_type FROM discovery_observations WHERE pmid = :pmid"),
        {"pmid": pmid},
    )
    assert grade == StudyType.SYSTEMATIC_REVIEW.value


# --- promotion goes through the console's own enqueue -----------------------


async def test_promotion_writes_the_run_and_the_decision_together(
    session: AsyncSession,
) -> None:
    """One transaction, so neither half can exist without the other.

    A run with no candidate update would be spend nobody can trace back to a
    proposal; a promoted candidate with no run violates
    ``discovery_candidates_promoted_has_run``. Both are prevented by doing the
    two writes in the request's transaction, which this exercises through the
    route function itself rather than reimplementing it.
    """
    from app.api.console.discovery import promote_candidate

    scan_id = await _make_scan(session)
    ui = await _make_descriptor(session, _FAKE_UIS[0])
    reviewer_id = await _make_user(session)
    candidate_id = str(uuid.uuid4())
    await session.execute(
        _INSERT_CANDIDATE,
        _candidate_params(id=candidate_id, scan_id=scan_id, substance_ui=ui),
    )
    await session.flush()

    reviewer = await session.get(User, reviewer_id)
    run = await promote_candidate(candidate_id, session, reviewer)

    assert run.topic == "creatine for cognition"
    assert run.status == RunStatus.QUEUED

    row = (
        await session.execute(
            sa.text(
                "SELECT c.status, c.pipeline_run_id, c.decided_by, r.origin, r.topic"
                " FROM discovery_candidates c"
                " JOIN pipeline_runs r ON r.id = c.pipeline_run_id"
                " WHERE c.id = :id"
            ),
            {"id": candidate_id},
        )
    ).one()
    assert row.status == DiscoveryCandidateStatus.PROMOTED.value
    assert str(row.decided_by) == reviewer_id
    # The topic is taken verbatim rather than recomposed at promote time: what
    # the reviewer read is what runs.
    assert row.topic == "creatine for cognition"
    # And the accounting column that makes "what has discovery cost us" one
    # WHERE against the token ledger.
    assert row.origin == RunOrigin.DISCOVERY.value


async def test_promoting_an_already_decided_candidate_is_a_conflict(
    session: AsyncSession,
) -> None:
    """409, not 404 — it exists and the reviewer can see it.

    Someone else acted on it, or a later scan expired it. "Not found" would send
    them looking for a bug that is not there.
    """
    from fastapi import HTTPException

    from app.api.console.discovery import promote_candidate

    scan_id = await _make_scan(session)
    ui = await _make_descriptor(session, _FAKE_UIS[0])
    reviewer_id = await _make_user(session)
    candidate_id = str(uuid.uuid4())
    await session.execute(
        _INSERT_CANDIDATE,
        _candidate_params(
            id=candidate_id,
            scan_id=scan_id,
            substance_ui=ui,
            status=DiscoveryCandidateStatus.EXPIRED.value,
        ),
    )
    await session.flush()

    reviewer = await session.get(User, reviewer_id)
    with pytest.raises(HTTPException) as caught:
        await promote_candidate(candidate_id, session, reviewer)
    assert caught.value.status_code == 409


# --- the write ordering ------------------------------------------------------


async def test_an_observation_needs_its_descriptor_first(session: AsyncSession) -> None:
    """``upsert_descriptors`` must run before ``record_observations``.

    Found by the first live scan, and the failure mode is the expensive one: the
    foreign key fires on the very first check tag, at the *end* of a scan that
    has already spent its whole PubMed budget. Nothing earlier in the run
    complains, because the ordering is a property of the caller rather than of
    either function.

    Pinned here rather than in the script because this is what makes it true —
    ``scripts/scan_trends.py`` orders the two calls correctly and a comment says
    why, but only the constraint can enforce it.
    """
    scan_id = await _make_scan(session)
    with pytest.raises(IntegrityError):
        await session.execute(
            sa.text(
                "INSERT INTO discovery_observations"
                " (descriptor_ui, pmid, entrez_date, is_substance)"
                " VALUES (:ui, :pmid, :date, true)"
            ),
            {
                "ui": _FAKE_UIS[2],  # never inserted into discovery_descriptors
                "pmid": _fake_pmid(),
                "date": date(2026, 8, 20),
                "scan": scan_id,
            },
        )
        await session.flush()


async def test_a_stoplisted_descriptor_is_never_an_angle(session: AsyncSession) -> None:
    """Only a descriptor classified ``OUTCOME`` can name what a substance is for.

    Stoplisted descriptors reach the ledger with ``is_substance`` false, exactly
    like real outcomes, so a filter on that flag alone offered "Antioxidants" as
    an angle and produced "dietary fiber for antioxidants": a topic no
    literature answers, which reached review as a ``supported`` article.
    """
    from app.discovery.service import DiscoveryService

    substance = await _make_descriptor(session, _FAKE_UIS[0])
    outcome = await _make_descriptor(
        session, _FAKE_UIS[1], kind=DiscoveryDescriptorKind.OUTCOME
    )
    stoplisted = await _make_descriptor(
        session, _FAKE_UIS[2], kind=DiscoveryDescriptorKind.STOPLISTED
    )
    statement = sa.text(
        "INSERT INTO discovery_observations"
        " (descriptor_ui, pmid, entrez_date, is_substance)"
        " VALUES (:ui, :pmid, :date, :is_substance)"
    )
    for _ in range(3):
        pmid = _fake_pmid()
        for ui, is_substance in ((substance, True), (outcome, False), (stoplisted, False)):
            params = {"ui": ui, "pmid": pmid, "date": date(2026, 9, 1)}
            await session.execute(statement, {**params, "is_substance": is_substance})
    await session.flush()

    cooccurrence, names = await DiscoveryService(session).angle_cooccurrence(
        [substance], date(2026, 8, 1), date(2026, 9, 30)
    )

    assert cooccurrence == {substance: {outcome: 3}}
    assert stoplisted not in names


async def test_pruning_keeps_what_the_scorer_reads_and_nothing_older(
    session: AsyncSession,
) -> None:
    """Retention: rows before the cutoff go, rows on or after it stay."""
    from app.discovery.service import DiscoveryService

    ui = await _make_descriptor(session, _FAKE_UIS[0])
    old, kept = _fake_pmid(), _fake_pmid()
    for pmid, when in ((old, date(2020, 1, 1)), (kept, date(2026, 9, 1))):
        await session.execute(
            sa.text(
                "INSERT INTO discovery_observations"
                " (descriptor_ui, pmid, entrez_date, is_substance)"
                " VALUES (:ui, :pmid, :date, true)"
            ),
            {"ui": ui, "pmid": pmid, "date": when},
        )
    await session.flush()

    await DiscoveryService(session).prune_observations(date(2026, 6, 1))

    rows = (
        await session.execute(
            sa.text("SELECT pmid FROM discovery_observations WHERE descriptor_ui = :ui"),
            {"ui": ui},
        )
    ).scalars().all()
    assert rows == [kept]
