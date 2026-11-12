"""Deterministic numbers for a social topic, and the momentum read off them. Pure.

Four rules, each against a way social metrics mislead:

* **Equal windows, aligned on when we collected.** Three 7-day windows ending
  now. A window counts only if collection succeeded (SUCCESS or EMPTY) on at
  least ``MIN_COVERED_DAYS`` of its days; otherwise every count in it is None.
  A week the API was down is unmeasured, never quiet.
* **Popularity is not growth.** Median views per day since upload says how big
  a topic is; upload and creator growth say whether it is getting bigger. Both
  are reported, under their own names.
* **Velocity needs two readings of the same post.** One observation of a view
  count says nothing about momentum; nothing is interpolated.
* **Missing is not zero.** A post with hidden likes is left out of the
  engagement rate, and the number of posts that rate rests on is reported.

The thresholds are guessed starting values, like the trend score's weights;
the result shows the numbers, not just the label.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.social.contracts import Momentum, TopicMetrics, WindowMetrics

ANALYTICS_VERSION = "social-v1"
WINDOW_DAYS = 7
WINDOWS = 3
MIN_COVERED_DAYS = 5
#: Added to both windows of a growth ratio: 0 -> 2 uploads is 1.4x, not infinite.
GROWTH_PRIOR = 5.0
#: Two readings closer than this make a noisy velocity.
MIN_VELOCITY_GAP = timedelta(hours=12)
RISING_GROWTH = 1.5
DECLINING_GROWTH = 0.67
RISING_MIN_CREATORS = 3


@dataclass(frozen=True, slots=True)
class Reading:
    observed_at: datetime
    views: int | None
    likes: int | None
    comments: int | None


@dataclass(frozen=True, slots=True)
class PostData:
    post_id: str
    platform: str
    creator_id: str | None
    published_at: datetime | None
    #: Oldest first.
    readings: tuple[Reading, ...] = ()


@dataclass(frozen=True, slots=True)
class Coverage:
    """Days each platform's collection answered, and days a search was truncated."""

    covered: dict[str, frozenset[date]] = field(default_factory=dict)
    truncated: dict[str, frozenset[date]] = field(default_factory=dict)


def windows(now: datetime) -> list[tuple[datetime, datetime]]:
    """Newest first: [now-7d, now), [now-14d, now-7d), [now-21d, now-14d)."""
    span = timedelta(days=WINDOW_DAYS)
    return [(now - span * (i + 1), now - span * i) for i in range(WINDOWS)]


def growth(current: int | None, previous: int | None) -> float | None:
    if current is None or previous is None:
        return None
    return round((current + GROWTH_PRIOR) / (previous + GROWTH_PRIOR), 3)


def compute(posts: Iterable[PostData], coverage: Coverage, *, now: datetime) -> TopicMetrics:
    posts = list(posts)
    platforms_collected = sorted(p for p, days in coverage.covered.items() if days)
    result_windows: list[WindowMetrics] = []
    for start, end in windows(now):
        days = _days(start, end)
        # A day counts when every platform that collected at all answered on it.
        covered_days = sum(
            1
            for day in days
            if platforms_collected
            and all(day in coverage.covered[p] for p in platforms_collected)
        )
        covered = covered_days >= MIN_COVERED_DAYS
        inside = [
            p for p in posts if p.published_at is not None and start <= p.published_at < end
        ]
        result_windows.append(
            WindowMetrics(
                start=start,
                end=end,
                covered=covered,
                covered_days=covered_days,
                uploads=len(inside) if covered else None,
                creators=len({p.creator_id for p in inside if p.creator_id})
                if covered
                else None,
                truncated=any(
                    day in coverage.truncated.get(p, frozenset())
                    for p in platforms_collected
                    for day in days
                ),
            )
        )
    w0, w1, w2 = result_windows
    upload_growth = growth(w0.uploads, w1.uploads)
    previous_growth = growth(w1.uploads, w2.uploads)
    current = [p for p in posts if p.published_at is not None and p.published_at >= w0.start]

    per_day, velocities, engagement = [], [], []
    for post in posts:
        latest = _latest_with_views(post.readings)
        if latest is not None and post.published_at is not None:
            age_days = max(
                (latest.observed_at - post.published_at).total_seconds() / 86_400, 1.0
            )
            per_day.append(latest.views / age_days)  # type: ignore[operator]
        velocity = _velocity(post.readings)
        if velocity is not None:
            velocities.append(velocity)
        rate = _engagement(post.readings)
        if rate is not None:
            engagement.append(rate)

    return TopicMetrics(
        version=ANALYTICS_VERSION,
        windows=result_windows,
        upload_growth=upload_growth,
        creator_growth=growth(w0.creators, w1.creators),
        acceleration=(
            round(upload_growth - previous_growth, 3)
            if upload_growth is not None and previous_growth is not None
            else None
        ),
        median_views_per_day=round(statistics.median(per_day), 1) if per_day else None,
        view_velocity=round(sum(velocities), 1) if velocities else None,
        velocity_posts=len(velocities),
        engagement_rate=round(statistics.median(engagement), 4) if engagement else None,
        engagement_posts=len(engagement),
        posts=len(posts),
        independent_creators=len({p.creator_id for p in current if p.creator_id}),
        platforms=sorted({p.platform for p in current}),
        platforms_collected=platforms_collected,
    )


def momentum(metrics: TopicMetrics) -> Momentum:
    w0, w1 = metrics.windows[0], metrics.windows[1]
    if not (w0.covered and w1.covered):
        return Momentum(
            status="insufficient_history",
            reason=(
                f"needs two covered weeks of daily collection (at least {MIN_COVERED_DAYS} "
                f"days each); this week has {w0.covered_days}, last week {w1.covered_days}"
            ),
        )
    creator_growth = metrics.creator_growth
    assert creator_growth is not None  # both windows covered
    detail = (
        f"{w0.creators} creators this week against {w1.creators} last week "
        f"({creator_growth:.2f}x); {w0.uploads} uploads against {w1.uploads}"
    )
    if creator_growth >= RISING_GROWTH and (w0.creators or 0) >= RISING_MIN_CREATORS:
        return Momentum(status="rising", reason=detail)
    if creator_growth <= DECLINING_GROWTH:
        return Momentum(status="declining", reason=detail)
    return Momentum(status="steady", reason=detail)


def limitations(metrics: TopicMetrics, all_platforms: Iterable[str]) -> list[str]:
    """What the reader of these numbers must know they do not cover."""
    notes = []
    missing = sorted(set(all_platforms) - set(metrics.platforms_collected))
    if missing:
        notes.append(f"not collected: {', '.join(missing)}")
    if any(w.truncated for w in metrics.windows):
        notes.append("some searches hit their page cap; upload counts are a floor")
    uncovered = [i for i, w in enumerate(metrics.windows) if not w.covered]
    if uncovered:
        notes.append(f"{len(uncovered)} of {WINDOWS} weeks lack enough collection days")
    if metrics.velocity_posts == 0:
        notes.append("no post has two readings yet; view velocity unmeasured")
    notes.append("popularity is not evidence: this says nothing about whether it works")
    return notes


def _days(start: datetime, end: datetime) -> list[date]:
    """The calendar days of (start, end]: today belongs to the current window."""
    first = start.date() + timedelta(days=1)
    return [first + timedelta(days=i) for i in range((end.date() - first).days + 1)]


def _latest_with_views(readings: tuple[Reading, ...]) -> Reading | None:
    return next((r for r in reversed(readings) if r.views is not None), None)


def _velocity(readings: tuple[Reading, ...]) -> float | None:
    with_views = [r for r in readings if r.views is not None]
    if len(with_views) < 2:
        return None
    last = with_views[-1]
    earlier = next(
        (
            r
            for r in reversed(with_views[:-1])
            if last.observed_at - r.observed_at >= MIN_VELOCITY_GAP
        ),
        None,
    )
    if earlier is None:
        return None
    days = (last.observed_at - earlier.observed_at).total_seconds() / 86_400
    return (last.views - earlier.views) / days  # type: ignore[operator]


def _engagement(readings: tuple[Reading, ...]) -> float | None:
    for reading in reversed(readings):
        if reading.views and reading.likes is not None and reading.comments is not None:
            return (reading.likes + reading.comments) / reading.views
    return None
