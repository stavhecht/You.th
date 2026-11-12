"""What the collection timer queues, and when a job is due.

The timer in the API process queues every scheduled worker job: the social and
product runs, the weekly retraction check, and the MeSH trend scan. Each has
its own switch. What these pin is the part that goes wrong quietly: a schedule
that fires on every restart, a naive timestamp that crashes the loop, or one
switch silently turning off a job it does not name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.social.contracts import CollectionKind
from app.social.schedule import INTERVALS, is_due, scheduled_intervals

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
DAY = timedelta(hours=24)


def _settings(*, collection: bool, scan: bool) -> Settings:
    return Settings(
        collection_schedule_enabled=collection,
        discovery_scan_enabled=scan,
        discovery_scan_interval_hours=24,
        _env_file=None,  # type: ignore[call-arg]
    )


# --- which jobs are scheduled ------------------------------------------------------


def test_both_switches_on_schedule_everything() -> None:
    assert scheduled_intervals(_settings(collection=True, scan=True)) == {
        **INTERVALS,
        CollectionKind.MESH_SCAN: DAY,
    }


def test_pausing_collection_keeps_the_trend_scan() -> None:
    """Turning off social collection (to save YouTube quota, say) used to stop
    the trend scan too, though DISCOVERY_SCAN_ENABLED still said true."""
    assert scheduled_intervals(_settings(collection=False, scan=True)) == {
        CollectionKind.MESH_SCAN: DAY
    }


def test_pausing_the_scan_keeps_collection() -> None:
    assert scheduled_intervals(_settings(collection=True, scan=False)) == INTERVALS


def test_both_off_schedules_nothing() -> None:
    assert scheduled_intervals(_settings(collection=False, scan=False)) == {}


# --- is_due -------------------------------------------------------------------------


def test_a_fresh_install_is_due() -> None:
    """Nothing has ever succeeded: the desk should fill without someone
    finding the button first."""
    assert is_due(None, DAY, now=NOW) is True


@pytest.mark.parametrize(
    ("age_hours", "expected"),
    [(25, True), (24, True), (23, False), (1, False)],
)
def test_dueness_is_measured_against_the_interval(age_hours: int, expected: bool) -> None:
    assert is_due(NOW - timedelta(hours=age_hours), DAY, now=NOW) is expected


def test_a_naive_timestamp_does_not_crash_the_scheduler() -> None:
    """A row written before timestamps carried a timezone would raise on the
    comparison and kill the loop permanently. The worst case of assuming UTC is
    one extra run; the worst case of raising is a desk that silently stops."""
    naive = (NOW - timedelta(hours=25)).replace(tzinfo=None)

    assert is_due(naive, DAY, now=NOW) is True
