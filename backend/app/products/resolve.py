"""Deterministic product identity and status. Pure: no I/O, no model.

**Identity** is ``brand|name`` after normalisation: case, accents, ™/®, the
brand repeated inside the name ("Oura Oura Ring 4"), and generation wording
("Gen 4", "4th generation" -> "4"). So "Oura Ring 4", "OURA Ring Gen 4" and
"Ring 4" from brand "Oura" are one product. A mention that names only a brand
has no product key and is not recorded: a brand is not a product.

**Status climbs and never falls**: uncertain (one source) -> corroborated
(two or more independent publishers or creators) -> confirmed (an official
page on the brand's own domain, or an FDA record). Retention deleting old
social mentions must not demote a product that was corroborated when seen.

**A launch needs more than a mention.** One source saying "launches" is
``claimed``; two independent ones, ``corroborated``; only a first FDA clearance
in the last year is ``confirmed``. A review or a mention is no launch at all.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from app.products.contracts import CATEGORY_INFO, CATEGORY_NAMES, ProductCategory

_MARKS_RE = re.compile(r"[™®©]")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_ORDINAL_RE = re.compile(r"^(?:gen(?:eration)?)?(\d+)(?:st|nd|rd|th)?$")
_GENERATION_WORDS = frozenset({"gen", "generation"})

STATUS_ORDER = ("uncertain", "corroborated", "confirmed")
LAUNCH_ORDER = ("none", "claimed", "corroborated", "confirmed")
_CATEGORIES = frozenset(c.value for c in ProductCategory)
#: "Announced" or "coming soon" is not launched, so only "launch" counts.
LAUNCH_KINDS = frozenset({"launch"})
#: How recent a product's first FDA clearance must be to count as a launch.
FDA_LAUNCH_DAYS = 365
#: A domain label is the brand, or the brand plus one of these ("ouraring.com").
_DOMAIN_SUFFIXES = (
    "",
    "ring",
    "health",
    "fit",
    "fitness",
    "labs",
    "tech",
    "official",
    "hq",
    "app",
    "inc",
    "co",
    "store",
    "shop",
    "sleep",
    "global",
    "usa",
)


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", _MARKS_RE.sub(" ", text))
    text = text.encode("ascii", "ignore").decode().lower()
    words = []
    for word in _NON_WORD_RE.sub(" ", text).split():
        match = _ORDINAL_RE.match(word)
        word = match.group(1) if match else word
        if word not in _GENERATION_WORDS:
            words.append(word)
    return words


#: Words saying what kind of thing a product is, not which one.
_KIND_WORDS = frozenset(
    form
    for phrase in (
        *(phrase for phrase, _ in CATEGORY_INFO.values()),
        *(word for words in CATEGORY_NAMES.values() for word in words),
        "smart watch wearable device ai",
    )
    for word in _tokens(phrase)
    for form in (word, f"{word}s")
)


def brand_key(brand: str) -> str:
    return " ".join(_tokens(brand))


def product_key(brand: str, name: str) -> str | None:
    """``oura|ring 4``, or None when the name adds nothing to the brand."""
    brand_words = _tokens(brand)
    words = _tokens(name)
    while brand_words and words[: len(brand_words)] == brand_words:
        words = words[len(brand_words) :]
    if not brand_words or not words:
        return None
    return f"{' '.join(brand_words)}|{' '.join(words)}"


def product_status(
    current: str, *, independent_sources: int, official_url: str | None, fda: bool
) -> str:
    if official_url or fda:
        found = "confirmed"
    elif independent_sources >= 2:
        found = "corroborated"
    else:
        found = "uncertain"
    return max(current, found, key=STATUS_ORDER.index)


def launch_status(
    current: str,
    *,
    launch_sources: int,
    fda_decision: datetime | None,
    now: datetime,
) -> str:
    if fda_decision is not None and now - fda_decision <= timedelta(days=FDA_LAUNCH_DAYS):
        found = "confirmed"
    elif launch_sources >= 2:
        found = "corroborated"
    elif launch_sources == 1:
        found = "claimed"
    else:
        found = "none"
    return max(current, found, key=LAUNCH_ORDER.index)


def is_official_url(url: str, brand: str) -> bool:
    """True when the URL's registrable label is the brand (or brand + a
    common suffix). ``ouraring.com`` is Oura's; ``appleinsider.com`` is not
    Apple's; a marketplace is nobody's. Strict on purpose: a miss leaves a
    product ``uncertain``, a false hit would call it confirmed."""
    compact = "".join(_tokens(brand))
    if len(compact) < 3:
        return False
    host = (urlsplit(url).hostname or "").lower()
    labels = host.split(".")
    if len(labels) < 2:
        return False
    label = labels[-2]
    if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net") and len(labels[-1]) == 2:
        label = labels[-3]
    label = label.replace("-", "")
    return any(label == compact + suffix for suffix in _DOMAIN_SUFFIXES)


def is_product_page(url: str, brand: str, name: str) -> bool:
    """True when the URL's path holds every word that says which product this is.

    A brand's page naming "Galaxy watches" in its title may be a category page
    (2026-11-07: 3 of a run's 5 new confirmations). Strict like ``is_official_url``:
    "Aina Ring" has no such word and an opaque ``/p/123`` holds none, so both stay
    unconfirmed, which a reviewer can see; "confirmed" never falls.
    """
    key = product_key(brand, name) or "|"
    words = [word for word in key.split("|")[1].split() if word not in _KIND_WORDS]
    path = "".join(_tokens(urlsplit(url).path))
    return bool(words) and all(word in path for word in words)


def checked_category(category: str, text: str) -> str:
    """The reader's category if the source's own text calls the product that, else "other"."""
    words = CATEGORY_NAMES.get(ProductCategory(category), ()) if category in _CATEGORIES else ()
    lowered = text.lower()
    if any(re.search(rf"\b{re.escape(word)}", lowered) for word in words):
        return category
    return ProductCategory.OTHER.value
