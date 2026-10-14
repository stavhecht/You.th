"""The research categories: exactly the article ``Subject`` values.

In its own module so the trend search contracts (``trends/contracts.py``) and
the research contracts can both use it without importing each other.
``research/contracts.py`` re-exports it, so ``from app.research.contracts
import Category`` keeps working.
"""

from __future__ import annotations

from enum import StrEnum

from app.domain.enums import Subject


# A research topic's category is the article category it would be filed under,
# so the members are exactly ``Subject``'s — checked below at import, so the
# desk's trending-topics filter, the feed drawer and the reviewer's picker
# cannot drift apart.
#
# A separate class rather than ``Category = Subject`` because a Pydantic enum's
# docstring becomes its JSON-schema ``description``, and ``TriageOutput``'s
# schema is the triage model's generation grammar: ``Subject``'s docstring
# (colours, pictures, reviewers) is maintainer rationale the model must not
# read. So this class deliberately has no docstring; comments only.
class Category(StrEnum):
    FITNESS = "fitness"
    NUTRITION = "nutrition"
    SUPPLEMENTS = "supplements"
    SLEEP_RECOVERY = "sleep_recovery"
    LIFESTYLE = "lifestyle"
    PREVENTIVE_HEALTH = "preventive_health"
    GENERAL_HEALTH = "general_health"
    WELLNESS = "wellness"
    OTHER = "other"


if [c.value for c in Category] != [s.value for s in Subject]:
    raise RuntimeError(
        "research Category must list exactly the article Subject values, in "
        "order; update app/research/categories.py with domain/enums.py"
    )
