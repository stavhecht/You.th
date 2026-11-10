"""When a substance must not be proposed again.

``suppression_reason`` is a pure function over history the caller has already
loaded — deliberately, so the dry run can *explain* a suppression without a
second query and so this file needs no database.

The rule it encodes is the difference between a proposal queue people read and
one they learn to ignore, and its two halves are deliberately asymmetric: a
promotion is permanent, a dismissal is conditional on both time and growth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.discovery.service import Decision, suppression_reason
from app.domain.enums import DiscoveryCandidateStatus

NOW = datetime(2026, 9, 4, tzinfo=UTC)
COOLOFF = 180


def _promoted(days_ago: int, *, produced_article: bool = True) -> Decision:
    return Decision(
        status=DiscoveryCandidateStatus.PROMOTED,
        decided_at=NOW - timedelta(days=days_ago),
        paper_count_at_decision=12,
        produced_article=produced_article,
    )


def _dismissed(days_ago: int, papers: int = 10) -> Decision:
    return Decision(
        status=DiscoveryCandidateStatus.DISMISSED,
        decided_at=NOW - timedelta(days=days_ago),
        paper_count_at_decision=papers,
    )


def _reason(history: list[Decision], current_count: int) -> str | None:
    return suppression_reason(
        history, current_count=current_count, now=NOW, cooloff_days=COOLOFF
    )


# --- nothing decided yet ------------------------------------------------------


def test_no_history_is_not_suppressed() -> None:
    assert _reason([], 17) is None


# --- promotion ----------------------------------------------------------------


def test_a_promotion_that_produced_an_article_is_permanent() -> None:
    """The flooding failure in its purest form.

    An article exists; the reviewer has no way to tell from the desk that they
    already said yes to this, so proposing it again wastes the attention the
    ranking exists to direct.
    """
    assert _reason([_promoted(400)], 90) is not None


def test_a_promotion_whose_run_failed_may_be_reproposed() -> None:
    """One bad topic string must not make a substance permanently unwritable.

    A run can die at ``UnanchoredQuery`` — non-retryable — or at a truncated
    synthesis, leaving nothing behind. There is then no article this would
    duplicate, and re-proposing it is the *only* way that topic ever gets
    another attempt.
    """
    assert _reason([_promoted(30, produced_article=False)], 17) is None


# --- dismissal: the cooloff gate ---------------------------------------------


def test_a_recent_dismissal_is_suppressed_however_much_it_grew() -> None:
    """A reviewer who said no last week should not be asked again this week."""
    assert _reason([_dismissed(10, papers=10)], 100) is not None


def test_the_reason_names_the_date_the_cooloff_ends() -> None:
    """The dry run prints this. "Suppressed" with no explanation is the output
    that makes someone delete the suppression logic."""
    reason = _reason([_dismissed(10)], 12)
    assert reason is not None
    assert "cooloff to" in reason
    assert "2027-02-21" in reason


# --- dismissal: the growth gate ----------------------------------------------


def test_an_old_dismissal_that_has_not_grown_stays_suppressed() -> None:
    """Time alone is not enough.

    A substance trickling along at ten papers a fortnight would otherwise come
    back every cooloff period with nothing new to say — which is exactly how a
    queue teaches people to stop reading it.
    """
    assert _reason([_dismissed(200, papers=10)], 15) is not None


def test_an_old_dismissal_that_doubled_comes_back() -> None:
    """Both gates open: the evidence base is materially different now."""
    assert _reason([_dismissed(200, papers=10)], 21) is None


def test_the_growth_gate_is_strictly_more_than_double() -> None:
    assert _reason([_dismissed(200, papers=10)], 20) is not None
    assert _reason([_dismissed(200, papers=10)], 21) is None


def test_a_dismissal_recorded_with_no_count_reopens_on_any_evidence() -> None:
    """Rows written before the rationale carried a count read as zero.

    Failing open here is the right direction: the alternative is a substance
    suppressed forever by a missing field, which is unrecoverable from the desk.
    """
    assert _reason([_dismissed(200, papers=0)], 4) is None


# --- ordering -----------------------------------------------------------------


def test_only_the_latest_decision_counts() -> None:
    """A substance can be dismissed, then promoted, then dismissed again.

    All three rows coexist — the unique index is partial on ``proposed`` — so
    the rule has to pick the most recent rather than the first it happens to
    load.
    """
    history = [_dismissed(400, papers=5), _promoted(10)]
    assert _reason(history, 50) is not None

    history = [_promoted(400), _dismissed(10)]
    reason = _reason(history, 50)
    assert reason is not None
    assert "dismissed" in reason


def test_history_order_does_not_matter() -> None:
    history = [_dismissed(400, papers=5), _promoted(10)]
    assert _reason(history, 50) == _reason(list(reversed(history)), 50)
