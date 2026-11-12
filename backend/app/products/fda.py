"""openFDA 510(k) lookups: does a cleared device match this product?

Free and keyless (``https://api.fda.gov/device/510k.json``; a key only raises
the daily limit). Asked only for categories where a device may need clearance
(``FDA_CATEGORIES``). A match is the strongest confirmation a product gets:
a public regulator's record with the applicant, device name and decision date.

openFDA answers 404 ``NOT_FOUND`` for a search with no results: that is EMPTY,
not an error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.retrieval.base import ProviderError, RateLimited
from app.retrieval.throttle import HttpClient
from app.social.contracts import CollectionError, CollectionStatus

URL = "https://api.fda.gov/device/510k.json"


async def lookup_510k(http: HttpClient, *, brand: str, name: str) -> dict[str, Any] | None:
    """The first 510(k) whose applicant names the brand and whose device name
    contains the product name, or None.

    The first, not the newest: updates get clearances of their own. Dexcom G7 was
    cleared in 2022 and again in 2026, and the newest would read as a launch this year.

    Raises:
        CollectionError: the lookup could not be answered.
    """
    search = f'device_name:"{_clean(name)}" AND applicant:"{_clean(brand)}"'
    try:
        response = await http.get(
            URL, params={"search": search, "limit": "5", "sort": "decision_date:asc"}
        )
    except RateLimited as exc:
        raise CollectionError(CollectionStatus.RATE_LIMITED, f"openFDA: {exc}") from exc
    except (ProviderError, httpx.HTTPError) as exc:
        raise CollectionError(CollectionStatus.ERROR, f"openFDA: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code == 429:
        raise CollectionError(CollectionStatus.RATE_LIMITED, "openFDA: HTTP 429")
    if response.status_code >= 400:
        raise CollectionError(CollectionStatus.ERROR, f"openFDA: HTTP {response.status_code}")
    try:
        results = response.json().get("results") or []
    except (ValueError, AttributeError) as exc:
        raise CollectionError(CollectionStatus.ERROR, "openFDA: unexpected response") from exc
    for result in results:
        applicant = str(result.get("applicant") or "")
        if brand.lower() in applicant.lower():
            decided = parse_date(result.get("decision_date"))
            return {
                "k_number": result.get("k_number"),
                "applicant": applicant,
                "device_name": result.get("device_name"),
                "decision_date": decided.isoformat() if decided else None,
            }
    return None


def parse_date(value: Any) -> datetime | None:
    """openFDA dates arrive as ``2024-03-05`` or ``20240305``."""
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _clean(text: str) -> str:
    """openFDA query syntax: no quotes or colons inside a phrase."""
    return " ".join(text.replace('"', " ").replace(":", " ").split())
