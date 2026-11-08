"""Social analytics: equal windows, coverage, growth vs popularity, missing is not zero."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.social import analytics
from app.social.analytics import Coverage, PostData, Reading
from app.social.weekly import too_broad

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


def _days(first: int, last: int) -> frozenset[date]:
    """Calendar days ``first``..``last`` days before NOW, inclusive."""
    return frozenset((NOW - timedelta(days=d)).date() for d in range(first, last + 1))


FULL = Coverage(covered={"youtube": _days(0, 21)})


def _post(i: int, *, days_ago: float, creator: str | None = None, readings=()) -> PostData:
    return PostData(
        post_id=f"p{i}",
        platform="youtube",
        creator_id=creator if creator is not None else f"c{i}",
        published_at=NOW - timedelta(days=days_ago),
        readings=tuple(readings),
    )


def test_an_uncovered_window_reports_none_not_zero() -> None:
    only_this_week = Coverage(covered={"youtube": _days(0, 6)})
    metrics = analytics.compute([_post(1, days_ago=1)], only_this_week, now=NOW)
    current, previous, _ = metrics.windows
    assert current.covered and current.uploads == 1
    assert not previous.covered
    assert previous.uploads is None and previous.creators is None
    assert metrics.upload_growth is None
    assert analytics.momentum(metrics).status == "insufficient_history"


def test_a_window_needs_five_collection_days() -> None:
    four_days = Coverage(covered={"youtube": _days(0, 3) | _days(7, 13)})
    metrics = analytics.compute([], four_days, now=NOW)
    assert metrics.windows[0].covered_days == 4
    assert not metrics.windows[0].covered
    assert metrics.windows[1].covered


def test_growth_uses_the_prior_and_acceleration_needs_three_windows() -> None:
    posts = [_post(i, days_ago=1) for i in range(5)] + [
        _post(10 + i, days_ago=9) for i in range(0)
    ]
    metrics = analytics.compute(posts, FULL, now=NOW)
    # 5 uploads against 0: (5 + 5) / (0 + 5) = 2.0, not infinite.
    assert metrics.upload_growth == 2.0
    assert metrics.acceleration == 2.0 - 1.0

    two_windows = Coverage(covered={"youtube": _days(0, 13)})
    assert analytics.compute(posts, two_windows, now=NOW).acceleration is None


def test_one_reading_gives_no_velocity_and_two_do() -> None:
    one = _post(1, days_ago=3, readings=[Reading(NOW, 1000, 10, 2)])
    metrics = analytics.compute([one], FULL, now=NOW)
    assert metrics.view_velocity is None and metrics.velocity_posts == 0

    two = _post(
        2,
        days_ago=3,
        readings=[Reading(NOW - timedelta(days=1), 1000, 10, 2), Reading(NOW, 3000, 20, 4)],
    )
    metrics = analytics.compute([one, two], FULL, now=NOW)
    assert metrics.view_velocity == 2000.0
    assert metrics.velocity_posts == 1


def test_readings_too_close_together_give_no_velocity() -> None:
    post = _post(
        1,
        days_ago=3,
        readings=[Reading(NOW - timedelta(hours=2), 1000, 1, 1), Reading(NOW, 1100, 1, 1)],
    )
    assert analytics.compute([post], FULL, now=NOW).view_velocity is None


def test_hidden_likes_are_left_out_of_engagement_not_counted_as_zero() -> None:
    hidden = _post(1, days_ago=2, readings=[Reading(NOW, 1000, None, 5)])
    shown = _post(2, days_ago=2, readings=[Reading(NOW, 1000, 90, 10)])
    metrics = analytics.compute([hidden, shown], FULL, now=NOW)
    assert metrics.engagement_rate == 0.1
    assert metrics.engagement_posts == 1


def test_one_creator_posting_ten_times_is_one_independent_creator() -> None:
    posts = [_post(i, days_ago=1, creator="same") for i in range(10)]
    metrics = analytics.compute(posts, FULL, now=NOW)
    assert metrics.independent_creators == 1
    assert metrics.windows[0].uploads == 10
    assert metrics.windows[0].creators == 1


def test_popular_but_flat_is_steady_not_rising() -> None:
    big = [Reading(NOW, 5_000_000, 1, 1)]
    posts = [_post(i, days_ago=1, readings=big) for i in range(4)] + [
        _post(10 + i, days_ago=9, readings=big) for i in range(4)
    ]
    metrics = analytics.compute(posts, FULL, now=NOW)
    assert metrics.median_views_per_day is not None and metrics.median_views_per_day > 1_000_000
    assert analytics.momentum(metrics).status == "steady"


def test_more_creators_than_last_week_is_rising_and_fewer_is_declining() -> None:
    rising = [_post(i, days_ago=1) for i in range(8)] + [_post(20, days_ago=9)]
    assert analytics.momentum(analytics.compute(rising, FULL, now=NOW)).status == "rising"
    falling = [_post(i, days_ago=9) for i in range(8)] + [_post(20, days_ago=1)]
    assert analytics.momentum(analytics.compute(falling, FULL, now=NOW)).status == "declining"


def test_a_truncated_search_is_flagged_in_its_window_and_in_the_limitations() -> None:
    coverage = Coverage(
        covered={"youtube": _days(0, 21)}, truncated={"youtube": frozenset({NOW.date()})}
    )
    metrics = analytics.compute([], coverage, now=NOW)
    assert metrics.windows[0].truncated and not metrics.windows[1].truncated
    notes = analytics.limitations(metrics, ["youtube", "tiktok"])
    assert any("floor" in n for n in notes)
    assert any("tiktok" in n for n in notes)
    assert any("not evidence" in n for n in notes)


def test_windows_are_equal_and_do_not_overlap() -> None:
    spans = analytics.windows(NOW)
    assert [end - start for start, end in spans] == [timedelta(days=7)] * 3
    assert spans[0][0] == spans[1][1] and spans[1][0] == spans[2][1]


@pytest.mark.parametrize(
    ("subject", "outcome"),
    [
        ("protein", ""),  # a seed
        ("stress", ""),
        ("smart ring", ""),  # a product category we search
        ("home gym", ""),  # part of one
        ("sleep", "sleep"),  # restates itself
        ("strength training", "strength"),
        ("running", "fitness"),  # an outcome that promises nothing
        ("protein food", ""),  # only generic words
    ],
)
def test_a_category_we_search_for_is_too_broad_to_propose(subject: str, outcome: str) -> None:
    assert too_broad(subject, outcome)


@pytest.mark.parametrize(
    ("subject", "outcome"),
    [
        ("protein", "muscle"),  # a real angle, though "muscle" is a generic word
        ("sleep", "brain function"),
        ("stress", "heart health"),
        ("smart ring", "sleep tracking"),
        ("moringa powder", ""),  # specific with no outcome
        ("nmn", "energy"),
    ],
)
def test_an_angle_or_a_specific_subject_is_kept(subject: str, outcome: str) -> None:
    assert not too_broad(subject, outcome)
