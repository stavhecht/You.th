"""The publisher feeds a trend run sweeps. The one place they are listed.

Like ``research/seeds.py``, this is an editorial decision, not tuning: these
lists decide which corners of the web a run can see. Every feed below answered
200 with fresh items when it was added (checked 2026-10-12). Feeds that refused
an automated reader (NIH, Mayo Clinic: 403) are deliberately absent rather than
fetched around their refusal.

A deployment can replace the list without a code change:
``RESEARCH_SOURCES_FILE`` names a JSON file shaped like
``{"feeds": [{"name", "url", "categories", "poll_minutes"}]}``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.research.categories import Category

logger = logging.getLogger(__name__)

C = Category


@dataclass(frozen=True, slots=True)
class Feed:
    name: str
    url: str
    #: The categories it mostly covers; the first is what its items are filed
    #: under before triage. A run sweeps only feeds overlapping its categories.
    categories: tuple[Category, ...]
    #: Do not refetch sooner than this; the stored items are replayed instead.
    poll_minutes: int = 60


FEEDS: tuple[Feed, ...] = (
    Feed(
        "bbc_health",
        "https://feeds.bbci.co.uk/news/health/rss.xml",
        (C.GENERAL_HEALTH, C.PREVENTIVE_HEALTH),
    ),
    Feed(
        "nyt_well",
        "https://rss.nytimes.com/services/xml/rss/nyt/Well.xml",
        (C.WELLNESS, C.LIFESTYLE, C.NUTRITION, C.FITNESS),
    ),
    Feed(
        "guardian_wellbeing",
        "https://www.theguardian.com/lifeandstyle/health-and-wellbeing/rss",
        (C.WELLNESS, C.LIFESTYLE),
    ),
    Feed(
        "guardian_fitness", "https://www.theguardian.com/lifeandstyle/fitness/rss", (C.FITNESS,)
    ),
    Feed("guardian_food", "https://www.theguardian.com/food/rss", (C.NUTRITION,)),
    Feed(
        "sciencedaily_health",
        "https://www.sciencedaily.com/rss/health_medicine.xml",
        (C.GENERAL_HEALTH, C.PREVENTIVE_HEALTH),
    ),
    Feed(
        "sciencedaily_nutrition",
        "https://www.sciencedaily.com/rss/health_medicine/nutrition.xml",
        (C.NUTRITION, C.SUPPLEMENTS),
    ),
    Feed(
        "sciencedaily_diet",
        "https://www.sciencedaily.com/rss/health_medicine/diet_and_weight_loss.xml",
        (C.NUTRITION,),
    ),
    Feed(
        "sciencedaily_fitness",
        "https://www.sciencedaily.com/rss/health_medicine/fitness.xml",
        (C.FITNESS,),
    ),
    Feed(
        "sciencedaily_sleep",
        "https://www.sciencedaily.com/rss/mind_brain/sleep_disorders.xml",
        (C.SLEEP_RECOVERY,),
    ),
    Feed(
        "sciencedaily_stress",
        "https://www.sciencedaily.com/rss/mind_brain/stress.xml",
        (C.LIFESTYLE, C.WELLNESS),
    ),
    Feed("runners_world", "https://www.runnersworld.com/rss/all.xml/", (C.FITNESS,)),
    Feed(
        "mens_health",
        "https://www.menshealth.com/rss/all.xml/",
        (C.FITNESS, C.NUTRITION, C.LIFESTYLE),
    ),
    Feed(
        "womens_health",
        "https://www.womenshealthmag.com/rss/all.xml/",
        (C.FITNESS, C.WELLNESS, C.NUTRITION),
    ),
    Feed(
        "prevention",
        "https://www.prevention.com/rss/all.xml/",
        (C.PREVENTIVE_HEALTH, C.WELLNESS),
    ),
    Feed("npr_health", "https://feeds.npr.org/1128/rss.xml", (C.GENERAL_HEALTH,)),
    Feed(
        "the_conversation_health",
        "https://theconversation.com/us/health/articles.atom",
        (C.GENERAL_HEALTH,),
    ),
    Feed(
        "news_medical",
        "https://www.news-medical.net/syndication.axd?format=rss",
        (C.GENERAL_HEALTH,),
    ),
    Feed("stat_news", "https://www.statnews.com/feed/", (C.GENERAL_HEALTH,)),
    Feed("outside", "https://www.outsideonline.com/feed/", (C.FITNESS, C.LIFESTYLE)),
    Feed(
        "stronger_by_science",
        "https://www.strongerbyscience.com/feed/",
        (C.FITNESS, C.SUPPLEMENTS),
        poll_minutes=720,
    ),
)


def feeds_for(feeds: tuple[Feed, ...], categories: list[Category]) -> list[Feed]:
    """Feeds overlapping the requested categories. ``OTHER`` alone sweeps all."""
    if list(categories) == [C.OTHER]:
        return list(feeds)
    wanted = set(categories)
    return [feed for feed in feeds if wanted & set(feed.categories)]


def load_sources_file(path: str) -> tuple[Feed, ...]:
    """The feed list, from ``path`` when set, else the defaults.

    A file that cannot be read or parsed is logged and ignored — a broken
    override must not take the defaults down with it.
    """
    if not path:
        return FEEDS
    try:
        data = json.loads(Path(path).read_text())
        return tuple(
            Feed(
                name=str(entry["name"]),
                url=str(entry["url"]),
                categories=tuple(Category(c) for c in entry.get("categories") or ["other"]),
                poll_minutes=int(entry.get("poll_minutes", 60)),
            )
            for entry in data["feeds"]
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.error("research sources file %s unusable, using defaults: %s", path, exc)
        return FEEDS
