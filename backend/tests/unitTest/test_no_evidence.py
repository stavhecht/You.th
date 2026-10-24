"""The no-evidence path: a topic with no usable literature still ships.

DESIGN.md §12 lists "retrieval finds nothing for a fringe trend" as mitigated
by ``no_evidence`` being a first-class verdict rather than an error. These
tests hold that claim to the code, end to end: the stage must not raise, the
draft must survive validation on its own merits rather than by exemption, and
the prose must read correctly for a plural trend as well as a singular product.
"""

from __future__ import annotations

import pytest

from app.domain.contracts import (
    CandidatePaper,
    ExtractionOutput,
    SynthesisInput,
    count_sentences,
)
from app.domain.enums import SourceApi, Verdict
from app.evidence.validation import validate_draft
from app.pipeline.stages import PipelineContext
from app.pipeline.steps.synthesize import SynthesizeStage
from app.services.card import derive_card
from app.services.no_evidence import HEADLINE_MAX_WORDS, no_evidence_draft
from app.services.tiptap import body_text_to_doc
from tests.conftest import FakeSession


class ExplodingClient:
    """A synthesis client that fails the test if it is ever called.

    The whole point of the deterministic branch is that no generative call is
    made on zero sources — a model handed a product name and nothing to ground
    it in is exactly the ungrounded generation this system exists to prevent.
    Asserting "did not raise" would pass just as well if the branch quietly
    called a model, so the client itself is the assertion.
    """

    async def synthesize(self, payload: SynthesisInput) -> object:
        raise AssertionError(
            "the model was called with an empty source set; the no-evidence "
            "branch was bypassed"
        )


def _context(product: str = "Ashwagandha", claims: list[str] | None = None) -> PipelineContext:
    return PipelineContext(
        run_id="run-1",
        topic=product,
        extraction=ExtractionOutput(
            product=product,
            target_claims=claims or ["reduces stress"],
            ingredients=[],
        ),
    )


# ---------------------------------------------------------------------------
# The stage branches instead of raising.
# ---------------------------------------------------------------------------


async def test_zero_sources_produces_a_draft_rather_than_a_stage_error() -> None:
    """The regression this module exists for.

    RETRIEVE deliberately lets an empty candidate set through so a fringe trend
    can be reported honestly. SYNTHESIZE used to raise on exactly that input,
    discarding it at the last step and turning the most useful thing this
    product can say into a failed run.
    """
    stage = SynthesizeStage(ExplodingClient())  # type: ignore[arg-type]

    result = await stage.run(_context())

    assert result.draft is not None
    assert result.draft.verdict is Verdict.NO_EVIDENCE
    assert result.synthesis_input is not None
    assert result.synthesis_input.sources == []


async def test_the_no_evidence_branch_spends_no_tokens() -> None:
    """No model call means no cost on precisely the topics worth least."""
    stage = SynthesizeStage(ExplodingClient())  # type: ignore[arg-type]

    result = await stage.run(_context())

    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.metrics["synthesize"]["model_called"] is False


@pytest.mark.parametrize(
    ("candidates", "expected_cause"),
    [
        ({}, "no_candidates_retrieved"),
        ({"reduces stress": []}, "no_candidates_retrieved"),
    ],
)
async def test_an_empty_candidate_pool_is_recorded_as_nothing_retrieved(
    candidates: dict[str, list[object]], expected_cause: str
) -> None:
    stage = SynthesizeStage(ExplodingClient())  # type: ignore[arg-type]
    ctx = _context().model_copy(update={"candidates": candidates})

    result = await stage.run(ctx)

    assert result.metrics["synthesize"]["no_evidence_cause"] == expected_cause


async def test_candidates_filtered_away_by_rank_is_a_distinct_cause() -> None:
    """Retrieved-then-filtered reads the same to a reader, not to us.

    Nothing published is the product working as intended; candidates retrieved
    and then filtered to nothing is a signal that ``min_year`` or the grade
    floor is tighter than the corpus can satisfy. The two must be tellable
    apart without reading two stages of metrics.
    """
    stage = SynthesizeStage(ExplodingClient())  # type: ignore[arg-type]
    paper = CandidatePaper(
        pmid="12345678",
        title="A trial of something",
        abstract="An abstract long enough to be a real one for these purposes.",
        source_api=SourceApi.PUBMED,
    )
    # Candidates present, but RANK kept none — so `ranked` is empty.
    ctx = _context().model_copy(
        update={"candidates": {"reduces stress": [paper]}, "ranked": {}}
    )

    result = await stage.run(ctx)

    metrics = result.metrics["synthesize"]
    assert metrics["no_evidence_cause"] == "all_candidates_filtered"
    assert metrics["candidates_retrieved"] == 1


# ---------------------------------------------------------------------------
# The draft passes validation on its merits, not by exemption.
# ---------------------------------------------------------------------------


async def test_the_no_evidence_draft_passes_validation() -> None:
    """Every check must be satisfiable by a zero-source article.

    Check 1 and 2 have an empty emitted set to work with, ``NO_EVIDENCE`` is
    exempt from the cited-beat rule, and ``best_grade([])`` is UNKNOWN, whose
    ceiling of 'weak' sits above 'no evidence'. If any of those stopped being
    true this article would land in ``validation_failed`` and never reach a
    human.
    """
    draft = no_evidence_draft("Ashwagandha", ["reduces stress"])
    payload = SynthesisInput(
        product="Ashwagandha", target_claims=["reduces stress"], sources=[]
    )

    report = await validate_draft(FakeSession([]), draft, payload)  # type: ignore[arg-type]

    assert report.passed
    assert report.citations_total == 0
    assert report.badge == "0/0 citations resolve"


def test_the_draft_cites_nothing() -> None:
    """It has nothing to cite, and inventing a citation is the cardinal sin."""
    draft = no_evidence_draft("Ashwagandha", ["reduces stress"])

    assert draft.all_cited_handles() == set()
    assert draft.citations == []


def test_the_draft_renders_to_a_document_and_a_card() -> None:
    """The template must survive the same assembly path as a generated draft."""
    draft = no_evidence_draft("Cold plunges", ["reduces muscle soreness"])

    doc = body_text_to_doc(draft.body)
    card = derive_card(draft.headline, doc, draft.verdict)

    assert len(doc["content"]) == 3
    assert card.verdict is Verdict.NO_EVIDENCE
    assert card.excerpt.startswith("Marketing for Cold plunges")


# ---------------------------------------------------------------------------
# The prose holds for the inputs extraction actually produces.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "product",
    [
        "Ashwagandha",
        "Cold plunges",
        "Greens powders",
        "Dr. Klein's Focus Blend",
        "NeuroGlow Ultra Advanced Daily Cognitive Support Complex Extra",
    ],
)
def test_every_field_stays_within_its_contract_bounds(product: str) -> None:
    """The bounds are Pydantic validators, so construction is the assertion.

    Parametrised over the shapes extraction really emits — a long branded name
    that would blow the 12-word headline, and an abbreviation whose full stop
    the sentence counter reads as a sentence end.
    """
    draft = no_evidence_draft(product, ["improves focus", "boosts memory"])

    assert len(draft.headline.split()) <= HEADLINE_MAX_WORDS
    assert count_sentences(draft.summary) <= 2
    for beat in (
        draft.body.beat_1_claim,
        draft.body.beat_2_evidence,
        draft.body.beat_3_bottom_line,
    ):
        assert count_sentences(beat) <= 3


def test_a_long_product_name_degrades_the_headline_rather_than_failing() -> None:
    """A 12-word ceiling must not fail a run that is otherwise correct."""
    draft = no_evidence_draft("A" + " Very" * 20 + " Long Supplement Name", ["works"])

    assert draft.headline == "No published evidence found"


@pytest.mark.parametrize(
    ("product", "claims", "expected"),
    [
        (
            "Cold plunges",
            ["reduces muscle soreness"],
            "Marketing for Cold plunges makes one claim: reduces muscle soreness.",
        ),
        (
            "Ashwagandha",
            ["reduces stress", "improves sleep quality"],
            "Marketing for Ashwagandha makes these claims: reduces stress and "
            "improves sleep quality.",
        ),
        (
            "Greens powders",
            ["improves gut health", "increases energy", "supports immunity"],
            "Marketing for Greens powders makes these claims: improves gut health, "
            "increases energy, and supports immunity.",
        ),
    ],
)
def test_beat_one_is_grammatical_for_plural_trends_and_singular_products(
    product: str, claims: list[str], expected: str
) -> None:
    """"Marketing for X" is the subject specifically so this holds.

    Roughly half the topics are trends and arrive plural, while extraction
    emits third-person-singular claim phrases. Any phrasing making the product
    the subject yields "Cold plunges is marketed on the claim that it reduces
    soreness" — the first sentence a reader sees, ungrammatical, in a product
    whose whole proposition is being more careful than the marketing it checks.
    """
    assert no_evidence_draft(product, claims).body.beat_1_claim == expected


def test_a_claim_carrying_a_full_stop_does_not_inflate_the_sentence_count() -> None:
    """Extraction is told to emit short phrases; it will not always comply."""
    draft = no_evidence_draft("Ashwagandha", ["reduces stress.", "improves sleep."])

    assert count_sentences(draft.body.beat_1_claim) == 1


def test_the_bottom_line_distinguishes_absent_evidence_from_contrary_evidence() -> None:
    """The load-bearing sentence in the module.

    "No evidence for" and "evidence against" are different statements, and a
    reader who takes the first for the second has been misled by an article
    that was technically accurate. Asserted rather than left to prose review
    because it is the one sentence whose loss would make this feature harmful
    rather than merely unhelpful.
    """
    bottom_line = no_evidence_draft("Ashwagandha", ["reduces stress"]).body.beat_3_bottom_line

    assert "not the same as evidence of no effect" in bottom_line
