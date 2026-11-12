"""The desk's scan state is read from the latest ``mesh_scan`` worker run.

The scan ran inside the API process until 2026-11-11; it is queued for the
worker now, and ``scan_out`` turns that run's row into what the desk reads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.console.discovery import scan_out
from app.api.console.schemas import ManualScanStatus

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def _run(**over: Any) -> Any:
    fields: dict[str, Any] = {
        "status": "completed",
        "created_at": NOW,
        "started_at": NOW,
        "finished_at": NOW,
        "stats": {},
        "notes": [],
        "error": None,
    }
    return SimpleNamespace(**(fields | over))


def test_no_run_yet_is_idle_and_still_says_when_a_scan_last_succeeded() -> None:
    out = scan_out(None, NOW)
    assert (out.status, out.last_scan_at) == (ManualScanStatus.IDLE, NOW)


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        # Queued reads as running: asked for and not finished, either way.
        ("queued", ManualScanStatus.RUNNING),
        ("running", ManualScanStatus.RUNNING),
        ("completed", ManualScanStatus.SUCCEEDED),
        ("failed", ManualScanStatus.FAILED),
    ],
)
def test_the_run_status_is_said_in_the_desks_words(
    stored: str, shown: ManualScanStatus
) -> None:
    assert scan_out(_run(status=stored), None).status is shown


def test_counts_notes_and_held_back_topics_reach_the_desk() -> None:
    out = scan_out(
        _run(
            stats={
                "records_seen": 412,
                "observations_written": 96,
                "candidates_proposed": 0,
                "suppressed": [["creatine for cognition", "promoted 2026-09-07"]],
            },
            notes=["baseline is shallow: 2 of 4 windows"],
        ),
        NOW,
    )
    # Zero proposals with 412 records is a quiet fortnight, not a broken harvest.
    assert (out.records_seen, out.candidates_proposed) == (412, 0)
    assert out.notes == ["baseline is shallow: 2 of 4 windows"]
    assert [(s.topic, s.reason) for s in out.suppressed] == [
        ("creatine for cognition", "promoted 2026-09-07")
    ]


def test_a_failed_run_carries_its_reason() -> None:
    out = scan_out(_run(status="failed", error={"message": "HTTPStatusError: 429"}), None)
    assert out.error == "HTTPStatusError: 429"
