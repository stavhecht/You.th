"""The console's manual scan runner.

Covers the three things about it that are not obvious from reading it: that a
second press cannot start a second scan, that a scan blowing up lands on the
state instead of anywhere else, and that the press itself never waits for the
work. No database and no network — ``run_scan`` is replaced, because what is
under test is the button, not the scan.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.discovery import manual as manual_module
from app.discovery.manual import ManualScanRunner, ManualScanStatus, ScanAlreadyRunning
from app.discovery.scan import ScanReport
from app.discovery.service import Window
from app.domain.enums import DiscoveryScanMode


def _report(**kwargs) -> ScanReport:
    return ScanReport(
        window=Window(date(2026, 1, 1), date(2026, 1, 15)),
        mode=DiscoveryScanMode.SCAN,
        applied=True,
        **kwargs,
    )


async def test_start_returns_before_the_scan_finishes(monkeypatch):
    """The press is 202, not a held connection: a scan is minutes long."""
    gate = asyncio.Event()

    async def slow_scan(**_kwargs):
        await gate.wait()
        return _report(records_seen=7, candidates_proposed=2)

    monkeypatch.setattr(manual_module, "run_scan", slow_scan)
    runner = ManualScanRunner()

    state = runner.start(settings=object(), factory=object())
    assert state.status is ManualScanStatus.RUNNING
    assert runner.is_running

    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert runner.state.status is ManualScanStatus.SUCCEEDED
    assert runner.state.records_seen == 7
    assert runner.state.candidates_proposed == 2
    assert runner.state.finished_at is not None


async def test_a_second_press_is_refused_while_one_runs(monkeypatch):
    """Single-flight. Two concurrent scans double this process's draw on NCBI's
    per-IP ceiling — shared with the worker — for the same answer twice."""
    gate = asyncio.Event()

    async def slow_scan(**_kwargs):
        await gate.wait()
        return _report()

    monkeypatch.setattr(manual_module, "run_scan", slow_scan)
    runner = ManualScanRunner()
    runner.start(settings=object(), factory=object())

    with pytest.raises(ScanAlreadyRunning):
        runner.start(settings=object(), factory=object())

    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # And it is pressable again once the first has landed.
    assert not runner.is_running


async def test_a_failing_scan_lands_on_the_state(monkeypatch):
    """PubMed falling over must not reach a reviewer as an unhandled task
    exception twenty seconds after they pressed the button."""

    async def broken_scan(**_kwargs):
        raise RuntimeError("ncbi said no")

    monkeypatch.setattr(manual_module, "run_scan", broken_scan)
    runner = ManualScanRunner()
    runner.start(settings=object(), factory=object())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert runner.state.status is ManualScanStatus.FAILED
    assert "ncbi said no" in (runner.state.error or "")
    assert not runner.is_running


async def test_notes_survive_onto_the_state(monkeypatch):
    """A shallow baseline is why the count is zero, and the desk shows it
    verbatim — the counts alone cannot distinguish "nothing is trending" from
    "this could not have proposed anything"."""

    async def scan(**_kwargs):
        return _report(notes=["baseline has 1 of 4 windows"])

    monkeypatch.setattr(manual_module, "run_scan", scan)
    runner = ManualScanRunner()
    runner.start(settings=object(), factory=object())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert runner.state.notes == ["baseline has 1 of 4 windows"]
