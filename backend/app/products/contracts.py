"""Product discovery's vocabulary.

``ProductCategory`` and ``MentionKind`` are part of the reader model's output
grammar (``intel/reader.py``), so they carry **no docstring**: Pydantic would
send it to the model as the enum's description. Same trap as ``Category``.
"""

from __future__ import annotations

from enum import StrEnum


class ProductCategory(StrEnum):
    SMART_RING = "smart_ring"
    GLUCOSE_MONITOR = "glucose_monitor"
    RECOVERY_WEARABLE = "recovery_wearable"
    LIGHT_THERAPY = "light_therapy"
    SMART_FITNESS_EQUIPMENT = "smart_fitness_equipment"
    NERVE_STIMULATION = "nerve_stimulation"
    UNDER_DESK_TREADMILL = "under_desk_treadmill"
    SLEEP_TRACKER = "sleep_tracker"
    MASSAGE_GUN = "massage_gun"
    BLUE_LIGHT_GLASSES = "blue_light_glasses"
    SMART_BEDDING = "smart_bedding"
    BREATHING_BIOFEEDBACK = "breathing_biofeedback"
    OTHER = "other"


class MentionKind(StrEnum):
    LAUNCH = "launch"
    ANNOUNCEMENT = "announcement"
    REVIEW = "review"
    COMPARISON = "comparison"
    MENTION = "mention"


#: Per category: the phrase searched for it (YouTube watch list, news), and the
#: words that relate it to a topic (``intel/related.py``). Editorial choices.
CATEGORY_INFO: dict[ProductCategory, tuple[str, tuple[str, ...]]] = {
    ProductCategory.SMART_RING: ("smart ring", ("sleep", "recovery", "hrv", "heart rate")),
    ProductCategory.GLUCOSE_MONITOR: (
        "continuous glucose monitor",
        ("glucose", "blood sugar", "insulin"),
    ),
    ProductCategory.RECOVERY_WEARABLE: (
        "recovery wearable",
        ("recovery", "hrv", "heart rate variability", "strain"),
    ),
    ProductCategory.LIGHT_THERAPY: (
        "LED light therapy mask",
        ("red light", "light therapy", "skin", "collagen"),
    ),
    ProductCategory.SMART_FITNESS_EQUIPMENT: (
        "smart home gym",
        ("home workout", "strength training", "home gym", "rowing"),
    ),
    ProductCategory.NERVE_STIMULATION: (
        "vagus nerve stimulation device",
        ("vagus", "vagal", "stress", "anxiety"),
    ),
    ProductCategory.UNDER_DESK_TREADMILL: (
        "under desk treadmill",
        ("walking", "steps", "sitting", "sedentary"),
    ),
    ProductCategory.SLEEP_TRACKER: ("sleep tracker", ("sleep",)),
    ProductCategory.MASSAGE_GUN: (
        "massage gun",
        ("massage", "muscle recovery", "soreness", "doms"),
    ),
    ProductCategory.BLUE_LIGHT_GLASSES: (
        "blue light glasses",
        ("blue light", "screen time", "eye strain"),
    ),
    ProductCategory.SMART_BEDDING: ("smart mattress", ("sleep", "mattress", "pillow")),
    ProductCategory.BREATHING_BIOFEEDBACK: (
        "breathing biofeedback device",
        ("breathing", "breathwork", "hrv", "stress"),
    ),
}

#: Categories where a device may need FDA clearance, so an openFDA record can
#: confirm it. Asking for the rest would spend requests on certain misses.
FDA_CATEGORIES = frozenset(
    {
        ProductCategory.GLUCOSE_MONITOR,
        ProductCategory.NERVE_STIMULATION,
        ProductCategory.LIGHT_THERAPY,
    }
)

#: Per category: word starts a product of it is called by. The reader's
#: category is kept only when the source's text has one (``resolve.checked_category``):
#: measured 2026-11-05, Bonsai filed watches and earbuds as smart rings, milk as
#: a massage gun and a home gym as a glucose monitor (which asks openFDA).
CATEGORY_NAMES: dict[ProductCategory, tuple[str, ...]] = {
    ProductCategory.SMART_RING: ("ring",),
    ProductCategory.GLUCOSE_MONITOR: ("glucose", "cgm"),
    ProductCategory.RECOVERY_WEARABLE: ("wearable", "band", "strap"),
    ProductCategory.LIGHT_THERAPY: ("light", "led"),
    ProductCategory.SMART_FITNESS_EQUIPMENT: (
        "gym",
        "rower",
        "rowing",
        "bike",
        "trainer",
        "machine",
        "dumbbell",
    ),
    ProductCategory.NERVE_STIMULATION: ("vagus", "vagal", "stimulat", "tens"),
    ProductCategory.UNDER_DESK_TREADMILL: ("treadmill", "walking pad"),
    ProductCategory.SLEEP_TRACKER: ("sleep",),
    ProductCategory.MASSAGE_GUN: ("massag",),
    ProductCategory.BLUE_LIGHT_GLASSES: ("glasses", "blue light"),
    ProductCategory.SMART_BEDDING: ("mattress", "pillow", "bed", "topper", "blanket"),
    ProductCategory.BREATHING_BIOFEEDBACK: ("breath", "biofeedback"),
}
