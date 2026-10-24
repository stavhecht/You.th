"""The trend scan scheduler.

The thing being automated *proposes*; it never enqueues, so nothing here can
spend a token — that is what makes it safe to leave on. What these pin is the
part that can still go wrong quietly: a scheduler that fires on every restart, or
one that dies on a bad tick and takes the desk back to manual without saying so.

No database. `is_due` is pure, and the loop is exercised against a fake session
factory so the tick logic can be tested without a live ledger.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.config import Settings
from app.discovery import schedule as schedule_module
from app.discovery.manual import ManualScanRunner, ScanAlreadyRunning
from app.discovery.schedule import ScanScheduler, is_due

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
DAY = timedelta(hours=24)


# ---------------------------------------------------------------------------
# is_due
# ---------------------------------------------------------------------------


def test_a_fresh_install_is_due() -> None:
    """No scan has ever succeeded. The desk should fill without someone finding
    the button first."""
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
    one extra scan; the worst case of raising is a desk that silently stops
    updating."""
    naive = (NOW - timedelta(hours=25)).replace(tzinfo=None)

    assert is_due(naive, DAY, now=NOW) is True


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, last: datetime | None) -> None:
        self._last = last

    async def scalar(self, *_: Any, **__: Any) -> datetime | None:
        return self._last

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


def _factory(last: datetime | None) -> Any:
    def make() -> _FakeSession:
        return _FakeSession(last)

    return make


def _settings(*, enabled: bool = True, interval_hours: int = 24) -> Settings:
    return Settings(
        discovery_scan_enabled=enabled,
        discovery_scan_interval_hours=interval_hours,
    )


class _SpyRunner:
    def __init__(self, *, running: bool = False, raises: bool = False) -> None:
        self.is_running = running
        self._raises = raises
        self.starts = 0

    def start(self, **_: Any) -> None:
        if self._raises:
            raise ScanAlreadyRunning("already")
        self.starts += 1


async def test_a_due_scan_is_started(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _SpyRunner()
    monkeypatch.setattr(schedule_module, "manual_scan", runner)
    scheduler = ScanScheduler(
        _settings(), _factory(NOW - timedelta(hours=30)), clock=lambda: NOW
    )

    await scheduler._tick_once(DAY)

    assert runner.starts == 1


async def test_a_recent_scan_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restart must not trigger a scan. Due-ness comes from the ledger, not
    from a timer since process start, so a deploy loop cannot become a scan
    loop."""
    runner = _SpyRunner()
    monkeypatch.setattr(schedule_module, "manual_scan", runner)
    scheduler = ScanScheduler(
        _settings(), _factory(NOW - timedelta(hours=1)), clock=lambda: NOW
    )

    await scheduler._tick_once(DAY)

    assert runner.starts == 0


async def test_a_scan_already_running_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent scans double this process's draw on NCBI's per-IP ceiling
    — shared with the worker — to compute the same answer twice."""
    runner = _SpyRunner(running=True)
    monkeypatch.setattr(schedule_module, "manual_scan", runner)
    scheduler = ScanScheduler(_settings(), _factory(None))

    await scheduler._tick_once(DAY)

    assert runner.starts == 0


async def test_racing_with_the_button_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A press can land between the is_running check and start(). The button
    wins and the tick does nothing, rather than the loop taking an exception."""
    runner = _SpyRunner(raises=True)
    monkeypatch.setattr(schedule_module, "manual_scan", runner)
    scheduler = ScanScheduler(_settings(), _factory(None))

    await scheduler._tick_once(DAY)  # must not raise


async def test_a_failing_tick_does_not_kill_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The symptom of a dead scheduler is *nothing happening*, which reads
    exactly like a quiet fortnight. One bad tick must not be able to cause it."""
    ticks = 0

    async def explode(_interval: timedelta) -> None:
        nonlocal ticks
        ticks += 1
        raise RuntimeError("database went away")

    scheduler = ScanScheduler(_settings(), _factory(None), tick_seconds=0.01)
    monkeypatch.setattr(scheduler, "_tick_once", explode)

    scheduler.start()
    await asyncio.sleep(0.05)
    await scheduler.stop()

    assert ticks > 1, "the loop kept ticking after an exception"


async def test_disabled_starts_no_task() -> None:
    scheduler = ScanScheduler(
        _settings(enabled=False), _factory(None), tick_seconds=0.01
    )

    scheduler.start()

    assert scheduler._task is None
    await scheduler.stop()  # still safe to call


async def test_stop_is_safe_before_start() -> None:
    await ScanScheduler(_settings(), _factory(None)).stop()


async def test_the_runner_is_the_same_one_the_button_uses() -> None:
    """Not a second code path. One runner means the single-flight guard covers
    both, and the desk's status line describes whichever scan is in flight
    without needing to know what started it."""
    from app.api.console import discovery

    assert schedule_module.manual_scan is discovery.manual_scan
    assert isinstance(schedule_module.manual_scan, ManualScanRunner)
