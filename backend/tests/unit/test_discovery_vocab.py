"""What counts as a substance, and the two ways that judgement went wrong.

Both failures below were found by running the scan against live PubMed rather
than by reasoning about it, and both produced a *plausible* ranking — which is
why they need tests rather than just fixes. A ranking topped by "Plant Extracts"
looks like a working trend detector until you notice it is the botanicals query
describing itself.

No database and no network: ``classify_descriptor`` takes three counts, and
``classify_all`` takes records.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from app.discovery.contracts import DescriptorHit, MeshRecord
from app.discovery.seeds import SEED_ANCHOR_UIS, SEED_QUERIES
from app.discovery.service import classify_all
from app.discovery.vocab import (
    BIOMARKER_UIS,
    BOTANICAL_UIS,
    STOPLIST_UIS,
    classify_descriptor,
)
from app.domain.enums import DiscoveryDescriptorKind

SUBSTANCE = DiscoveryDescriptorKind.SUBSTANCE
OUTCOME = DiscoveryDescriptorKind.OUTCOME
STOPLISTED = DiscoveryDescriptorKind.STOPLISTED

#: A descriptor tagged exactly like a real intervention: in every paper's
#: chemical list, carrying an intervention qualifier throughout. Anything that
#: still comes back non-substance was refused by a list, not by the rule.
PERFECT_SUBSTANCE_SIGNALS = {
    "papers": 20,
    "chemical_list_papers": 20,
    "intervention_qualifier_papers": 20,
}


def _classify(ui: str, **overrides: int) -> DiscoveryDescriptorKind:
    return classify_descriptor(ui, **{**PERFECT_SUBSTANCE_SIGNALS, **overrides})


# --- the seed-anchor bug ----------------------------------------------------


@pytest.mark.parametrize("ui", sorted(SEED_ANCHOR_UIS))
def test_a_seed_anchor_is_never_a_substance(ui: str) -> None:
    """The bug this caught: "Plant Extracts" ranked first among botanicals.

    Measured 2026-09-04 — 146 of 665 papers, which is the botanicals seed
    query describing itself. An anchor is on every record in its net by
    construction, so it cannot distinguish any part of it, and it passes the
    substance rule cleanly because it genuinely *is* a chemical people take.

    Perfect substance signals here on purpose: the anchor rule has to win
    against the strongest possible evidence, not merely break a tie.
    """
    assert _classify(ui) is STOPLISTED


def test_every_mesh_term_a_seed_names_has_an_anchor() -> None:
    """The drift this design exists to prevent, enforced rather than documented.

    Anchors live beside the terms in ``seeds.py`` so the two cannot separate —
    but "cannot" is only true if something checks. Adding a term to a seed and
    forgetting its UI puts that subject straight back at the top of its own
    ranking, silently and plausibly.
    """
    for seed in SEED_QUERIES:
        named = re.findall(r'"([^"]+)"\[MeSH\]', seed.terms)
        assert len(seed.anchors) == len(named), (
            f"seed {seed.name!r} names {len(named)} MeSH terms {named} but "
            f"declares {len(seed.anchors)} anchor(s) — add the missing UI"
        )


def test_anchors_are_derived_from_the_seeds() -> None:
    """Restating them in a stoplist is what the union exists to avoid."""
    assert frozenset().union(*(s.anchors for s in SEED_QUERIES)) == SEED_ANCHOR_UIS
    # And no anchor is duplicated into the hand list, which would make editing
    # a seed look effective while the stale copy kept matching.
    assert not (SEED_ANCHOR_UIS & set(STOPLIST_UIS))


# --- the category-abstraction bug -------------------------------------------


@pytest.mark.parametrize(
    "ui",
    ["D000970", "D000900", "D007004", "D001688", "D004365", "D002214", "D013607"],
)
def test_a_category_or_dosage_form_is_never_a_substance(ui: str) -> None:
    """The second live failure: five of the top twenty were these.

    "Antineoplastic Agents" and "Capsules" pass the substance rule cleanly —
    they are chemicals and they carry /therapeutic use — and neither is a thing
    anyone takes. A capsule is how a substance arrives; an action class names a
    set, not a member.

    This is a mitigation, not a solution: the list covers what actually showed
    up, and the real fix is a MeSH tree lookup against D27 (Chemical Actions and
    Uses). A new category will slip through until then, which is why the
    document-frequency ceiling below is the backstop.
    """
    assert _classify(ui) is STOPLISTED


# --- the rule itself ---------------------------------------------------------


def test_a_biomarker_is_demoted_despite_being_a_chemical() -> None:
    """Cortisol is measured, not taken.

    Checked *before* the chemical-list test, because that is the check it exists
    to override — the failure it prevents is a proposal for "cortisol for
    stress", an outcome wearing an intervention's clothes.
    """
    for ui in BIOMARKER_UIS:
        assert _classify(ui) is OUTCOME


def test_a_botanical_is_promoted_despite_no_chemical_list() -> None:
    """MeSH indexes botanicals as organisms, so the rule cannot see them.

    Without the allowlist every one of them classifies as an outcome, losing
    exactly the category this product writes about most.
    """
    for ui in BOTANICAL_UIS:
        assert _classify(ui, chemical_list_papers=0, intervention_qualifier_papers=0) is (
            SUBSTANCE
        )


def test_the_rule_is_conjunctive() -> None:
    """Both signals, not either.

    A missed substance is a topic nobody writes this fortnight — recoverable,
    and the trend will still be there. A false one wastes a reviewer's attention
    and, if promoted, spends a run on an article about a blood measurement.
    """
    unlisted = "D999999"
    assert _classify(unlisted) is SUBSTANCE
    # In no chemical list: not a substance however it is qualified.
    assert _classify(unlisted, chemical_list_papers=0) is OUTCOME
    # A chemical nobody administers: measured, not taken.
    assert _classify(unlisted, intervention_qualifier_papers=0) is OUTCOME


def test_the_qualifier_ratio_is_a_threshold_not_a_presence_test() -> None:
    """One paper out of sixty saying /administration & dosage is noise."""
    unlisted = "D999999"
    assert _classify(unlisted, papers=20, intervention_qualifier_papers=5) is SUBSTANCE
    assert _classify(unlisted, papers=20, intervention_qualifier_papers=4) is OUTCOME


def test_a_descriptor_seen_on_no_papers_is_not_a_substance() -> None:
    """Guards the division. Reachable if a caller aggregates an empty window."""
    assert _classify("D999999", papers=0) is OUTCOME


# --- the corpus-level backstop -----------------------------------------------


def _record(pmid: str, *uis: str) -> MeshRecord:
    return MeshRecord(
        pmid=pmid,
        entrez_date=date(2026, 9, 4),
        descriptors=[
            DescriptorHit(
                ui=ui,
                name=f"Descriptor {ui}",
                in_chemical_list=True,
                intervention_qualifier=True,
            )
            for ui in uis
        ],
    )


def test_a_ubiquitous_descriptor_is_stoplisted_by_frequency() -> None:
    """What keeps the curated lists from having to be complete.

    MeSH has ~30,000 descriptors and check tags change between editions, so a
    hand list will always be missing something — and what it misses has a
    constant shape: a term on most of the corpus, which by construction cannot
    distinguish any part of it. This is the rule that catches the *next*
    category abstraction before anyone notices it.
    """
    records = [_record(str(n), "D999901") for n in range(10)]
    records[0].descriptors.append(
        DescriptorHit(
            ui="D999902",
            name="Rare",
            in_chemical_list=True,
            intervention_qualifier=True,
        )
    )
    kinds, names = classify_all(records, frequency_ceiling=0.35)

    assert kinds["D999901"] is STOPLISTED  # 10 of 10
    assert kinds["D999902"] is SUBSTANCE  # 1 of 10
    assert names["D999901"] == "Descriptor D999901"


def test_the_ratio_is_computed_over_distinct_papers() -> None:
    """A paper tagging a descriptor twice is still one paper's worth of evidence."""
    records = [_record(str(n), "D999901") for n in range(4)]
    for record in records[:3]:
        record.descriptors[0].intervention_qualifier = False
    # Ceiling of 1.0 takes the frequency rule out of the way: this descriptor is
    # on every record, so any lower ceiling stoplists it before the ratio is
    # ever consulted — which is the ordering, and worth knowing when a test here
    # fails for a reason it does not seem to be about.
    kinds, _ = classify_all(records, frequency_ceiling=1.0)
    # 1 of 4 qualified = 0.25, exactly the threshold.
    assert kinds["D999901"] is SUBSTANCE


def test_classify_all_of_nothing_is_empty() -> None:
    """A seed can legitimately match no records over a fortnight."""
    assert classify_all([], frequency_ceiling=0.35) == ({}, {})
