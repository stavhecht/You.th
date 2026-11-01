"""Generated imagery: the prompt's content rules, and never failing a run.

Two things are pinned here, and they are the two that would be expensive to
discover in production.

**A decoration must not be able to fail a run.** Every way an image can go
wrong — no key, a 503, a render too big for the store, a bug in our own encoder
— has to come back as a context with ``illustration is None`` and a recorded
cause, never as a raised ``StageError``. The article is complete and citable
without a picture; trading it for one is the wrong side of that bargain.

**A generated picture must not become a claim.** The prompt is a locked
template precisely so that it can be tested, and the tests below assert what
must never appear in it: clinical settings, before-and-after framing, posed or
transformed bodies — and the verdict, which is the one input that would make
the picture an argument about the evidence.

A person may appear, on the one path where the subject *is* an activity, and
the tests split accordingly. Read ``_BANNED_EVERYWHERE`` first: it is the list
that does not move, and the point of the split is that letting a body into a
yoga photograph relaxed the body ban and nothing else.

No database is needed. The image client is a bare duck-typed fake, the idiom
``test_no_evidence.py`` uses for the same job.
"""

from __future__ import annotations

import io
import math
import re
from collections import Counter

import pytest

from app.domain.contracts import (
    ArticleBody,
    ExtractionOutput,
    Illustration,
    SynthesisOutput,
)
from app.domain.enums import ImageFrame, Subject, Verdict
from app.imagery.base import ImageError, ImageRequest, RenderedImage
from app.imagery.prompt import (
    EXCLUSIONS,
    EXCLUSIONS_WITH_PEOPLE,
    NEGATIVE_PROMPT,
    TREATMENT,
    Motif,
    build_alt_text,
    build_prompt,
    infer_motif_subject,
    scrub_subject,
)
from app.llm.base import TokenUsage
from app.pipeline.stages import PipelineContext, StageError, StageName
from app.pipeline.steps.illustrate import IllustrateStage, IllustrationConfig
from app.services.illustration import (
    fresh_seed,
    generate_illustration,
    seed_for_run,
)
from app.services.media import MEDIA_SRC_RE
from tests.conftest import FakeMediaSession

LEAD = (1216, 832)
COVER = (864, 1152)


def _webp(width: int = 8, height: int = 8, seed: int = 0) -> bytes:
    """Real WebP bytes, so ``store_image``'s sniffer has something to read.

    The seed tints the picture so two renders differ in their bytes, and
    therefore in the path the content-addressed store gives them. Without that,
    a redraw would land on the file it was replacing and every test about
    redrawing would pass by accident.
    """
    from PIL import Image

    buffer = io.BytesIO()
    tint = (200, 190, (175 + seed) % 256)
    Image.new("RGB", (width, height), tint).save(buffer, format="WEBP")
    return buffer.getvalue()


class _FakeImageClient:
    """Answers with real bytes and records what it was asked for."""

    model_id = "huggingface/black-forest-labs/FLUX.1-schnell"

    def __init__(
        self, *, fail_on: int | set[int] | None = None, oversized: bool = False
    ) -> None:
        self.requests: list[ImageRequest] = []
        # Which requests (1-based) fail. A set, because the stage retries a
        # failed render once, so "the cover always fails" is two requests.
        self._fail_on = {fail_on} if isinstance(fail_on, int) else (fail_on or set())
        self._oversized = oversized

    async def render(self, request: ImageRequest) -> RenderedImage:
        self.requests.append(request)
        if len(self.requests) in self._fail_on:
            raise ImageError("provider returned 503")
        data = _webp(request.width // 100, request.height // 100, request.seed)
        if self._oversized:
            # Trailing bytes a WebP decoder ignores; the size gate does not.
            data += b"\x00" * 10_000
        return RenderedImage(
            data=data,
            content_type="image/webp",
            width=request.width,
            height=request.height,
            seed=request.seed,
        )


class _ExplodingImageClient:
    """Fails the test if it is ever called."""

    model_id = "huggingface/never"

    async def render(self, request: ImageRequest) -> RenderedImage:
        raise AssertionError("the image provider was called on a disabled pipeline")


def _config(
    *, enabled: bool = True, max_bytes: int = 8 * 1024 * 1024, cover_from_lead: bool = False
):
    return IllustrationConfig(
        max_bytes=max_bytes,
        lead_size=LEAD,
        cover_size=COVER,
        enabled=enabled,
        cover_from_lead=cover_from_lead,
    )


def _context(product: str = "Ashwagandha KSM-66") -> PipelineContext:
    return PipelineContext(
        run_id="run-1",
        topic=f"{product} for stress",
        extraction=ExtractionOutput(
            product=product, target_claims=["reduces stress"], ingredients=["ashwagandha"]
        ),
        draft=SynthesisOutput(
            headline="Ashwagandha does not lower evening cortisol",
            verdict=Verdict.WEAK,
            summary="One small trial.",
            body=ArticleBody(
                beat_1_claim="It claims to lower stress.",
                beat_2_evidence="One small trial found nothing.",
                beat_3_bottom_line="Not enough to say.",
            ),
        ),
    )


# --- the pair, and the store ------------------------------------------------


async def test_a_generated_pair_lands_in_the_context() -> None:
    client = _FakeImageClient()
    result = await IllustrateStage(client, _config(), FakeMediaSession()).run(_context())

    assert result.illustration is not None
    assert MEDIA_SRC_RE.match(result.illustration.lead.src)
    assert MEDIA_SRC_RE.match(result.illustration.cover.src)
    assert result.illustration.lead.model == client.model_id
    assert result.illustration.cover.model == client.model_id
    assert result.metrics["illustrate"]["generated"] == 2
    assert result.metrics["illustrate"]["cause"] is None


async def test_the_two_frames_are_drawn_at_the_two_configured_shapes(
) -> None:
    """Landscape for the prose column, portrait for the tile — from one prompt.

    Asserts what we control: both frames go out under the *same* prompt and the
    same seed. It does not assert they come back looking alike — the provider
    ignores the seed, and the two compositions genuinely differ. See
    services/illustration.py.
    """
    client = _FakeImageClient()
    await IllustrateStage(client, _config(), FakeMediaSession()).run(_context())

    lead, cover = client.requests
    assert (lead.width, lead.height) == LEAD
    assert (cover.width, cover.height) == COVER
    # One prompt and one seed is what makes the cover a *reframing* of the
    # lead rather than an independent second picture — the whole basis of the
    # pairing rule in services/card.py.
    assert lead.prompt == cover.prompt
    assert lead.seed == cover.seed


async def test_the_pair_is_atomic() -> None:
    """A cover with no lead is the one state derive_card cannot reason about."""
    # The cover fails on the first attempt (request 2) and on the retry (4).
    stage = IllustrateStage(_FakeImageClient(fail_on={2, 4}), _config(), FakeMediaSession())
    result = await stage.run(_context())

    assert result.illustration is None
    assert result.metrics["illustrate"]["generated"] == 0


# --- never failing the run --------------------------------------------------


async def test_the_run_survives_a_failed_generation() -> None:
    """The central promise of this stage."""
    stage = IllustrateStage(_FakeImageClient(fail_on={1, 2}), _config(), FakeMediaSession())

    result = await stage.run(_context())  # must not raise

    assert result.illustration is None
    assert result.metrics["illustrate"]["cause"] == "provider_error"
    assert "503" in result.metrics["illustrate"]["detail"]
    assert result.metrics["illustrate"]["attempts"] == 2


async def test_a_failed_render_is_retried_once() -> None:
    """A cold provider's first 503 should cost a retry, not the picture."""
    client = _FakeImageClient(fail_on=1)
    result = await IllustrateStage(client, _config(), FakeMediaSession()).run(_context())

    assert result.illustration is not None
    assert result.metrics["illustrate"]["attempts"] == 2
    assert len(client.requests) == 3  # the failed lead, then lead and cover


async def test_crop_mode_bills_one_render_and_cuts_the_cover_from_the_lead() -> None:
    """IMAGE_COVER_MODE=crop: one landscape render, the portrait cover cropped out."""
    client = _FakeImageClient()
    stage = IllustrateStage(client, _config(cover_from_lead=True), FakeMediaSession())
    result = await stage.run(_context())

    assert len(client.requests) == 1
    assert (client.requests[0].width, client.requests[0].height) == LEAD
    assert result.illustration is not None
    cover = result.illustration.cover
    # The cover's shape is read off the cropped bytes: 3:4, like the tile.
    assert abs(cover.width / cover.height - COVER[0] / COVER[1]) < 0.02
    assert cover.src != result.illustration.lead.src
    assert result.metrics["illustrate"]["renders"] == 1


async def test_a_revision_pass_keeps_the_pictures_already_drawn() -> None:
    """VALIDATE -> SYNTHESIZE comes back through ILLUSTRATE; it must not re-bill."""
    first = await IllustrateStage(_FakeImageClient(), _config(), FakeMediaSession()).run(
        _context()
    )
    again = await IllustrateStage(
        _ExplodingImageClient(), _config(), FakeMediaSession()
    ).run(first.model_copy(update={"revise_round": 1}))

    assert again.illustration == first.illustration
    assert again.metrics["illustrate"]["cause"] == "kept_from_first_draft"


async def test_an_absent_client_records_why_rather_than_going_quiet(
) -> None:
    """"No key" and "switched off" are the same blank tile, different problems."""
    no_key = await IllustrateStage(None, _config(), FakeMediaSession()).run(_context())
    assert no_key.illustration is None
    assert no_key.metrics["illustrate"]["cause"] == "not_configured"

    off = await IllustrateStage(
        None, _config(enabled=False), FakeMediaSession()
    ).run(_context())
    assert off.metrics["illustrate"]["cause"] == "disabled"


async def test_a_render_too_large_for_the_store_is_recorded_not_raised(
) -> None:
    """store_image has no size gate; services/illustration.py is the only one."""
    stage = IllustrateStage(
        _FakeImageClient(oversized=True), _config(max_bytes=512), FakeMediaSession()
    )
    result = await stage.run(_context())

    assert result.illustration is None
    assert result.metrics["illustrate"]["cause"] == "provider_error"
    assert "512" in result.metrics["illustrate"]["detail"]


async def test_a_missing_draft_is_still_a_stage_error() -> None:
    """The one exception: that means SYNTHESIZE did not run, not that an image failed."""
    ctx = PipelineContext(run_id="run-1", topic="anything")
    with pytest.raises(StageError):
        await IllustrateStage(_FakeImageClient(), _config(), FakeMediaSession()).run(ctx)


async def test_illustration_spends_no_entry_in_the_token_ledger(
) -> None:
    """Images are outside the ledger, like embeddings.

    A model id recorded against four zero token counts would price a paid call
    as free — the exact mistake ``llm/pricing.py`` refuses to make.
    """
    result = await IllustrateStage(
        _FakeImageClient(), _config(), FakeMediaSession()
    ).run(_context())

    assert str(StageName.ILLUSTRATE) not in result.usage_by_stage
    assert result.usage == TokenUsage()


# --- seeds ------------------------------------------------------------------


def test_the_seed_is_stable_across_attempts_of_one_run() -> None:
    """Our half of "a requeued draft redraws the same picture".

    The function is deterministic; whether it has any effect is the provider's
    choice, and the current one ignores seeds. Pinned anyway, because the day
    IMAGE_INFERENCE_PROVIDER points at fal-ai or replicate this starts to bite.
    """
    assert seed_for_run("run-abc") == seed_for_run("run-abc")
    assert seed_for_run("run-abc") != seed_for_run("run-def")


def test_regenerating_rolls_a_new_seed() -> None:
    """Pressing regenerate means "not that one"."""
    assert len({fresh_seed() for _ in range(20)}) > 1


# --- the content rules ------------------------------------------------------

#: Never allowed on the still-life path, which is every subject that is a thing
#: you take rather than a thing you do.
_BANNED_IN_PROMPT = (
    "person",
    "people",
    "human",
    "face",
    "hand",
    "body",
    "patient",
    "doctor",
    "nurse",
    "clinic",
    "hospital",
    "laborator",
    "before and after",
    "chart",
    "graph",
)

#: Never allowed on **either** path. These are the claim-shaped ones: a body in
#: frame was only ever a proxy for them, so allowing the body must not relax a
#: single entry here. Whatever is dropped from ``_BANNED_IN_PROMPT`` for the
#: people path has to be a body word and nothing else.
_BANNED_EVERYWHERE = (
    "patient",
    "doctor",
    "nurse",
    "clinic",
    "hospital",
    "laborator",
    "before and after",
    "chart",
    "graph",
)

#: Never allowed on the people path either — the specific forms in which a
#: person in a wellness image states a result.
_BANNED_WITH_PEOPLE = (
    "bodybuilder",
    "fitness model",
    "six-pack",
    "bare torso",
    "weight loss",
    "stock photo",
)

_OBJECT_PRODUCTS = [
    "Ashwagandha KSM-66",
    "Dr. Klein's Focus Blend",
    "NeuroGlow Ultra Advanced Daily Cognitive Support Complex",
    "Red light therapy panel",
    "Retinol cream",
]

#: Activities, so the people path is open for them.
_PEOPLE_PRODUCTS = [
    "Cold plunge",
    "Hot yoga",
    "Intermittent fasting",
    "Resistance training",
]


@pytest.mark.parametrize("product", _OBJECT_PRODUCTS)
def test_the_prompt_never_asks_for_a_person_or_a_clinic(product: str) -> None:
    """The content-safety test, on the still-life path.

    A picture of someone looking healthier beside a supplement is an efficacy
    claim no citation backs, made in the register readers do not read
    critically. Every banned term must appear only as an exclusion.
    """
    prompt, negative = build_prompt(product)
    positive_scene = prompt.replace(EXCLUSIONS, "").lower()

    for term in _BANNED_IN_PROMPT:
        # Anchored at a word start, so "graph" does not fire on "photograph"
        # while "laborator" still catches "laboratory". Unanchored substrings
        # would make this test unmaintainable and, worse, quietly weakened.
        pattern = rf"\b{re.escape(term)}"
        assert not re.search(pattern, positive_scene), f"{term!r} reached the prompt"
        assert re.search(pattern, negative.lower()), (
            f"{term!r} missing from the negative prompt"
        )


@pytest.mark.parametrize("product", _PEOPLE_PRODUCTS)
def test_the_people_path_still_refuses_every_claim_shaped_thing(
    product: str,
) -> None:
    """Allowing a body must relax the body ban and nothing else.

    A protocol is an activity, so someone mid-stretch depicts the subject. A
    clinic, a before-and-after pair or a posed physique depicts a *result*, and
    those were never about whether a person was in the frame — which is why
    every one of them has to survive the change that let the person in.
    """
    prompt, negative = build_prompt(product)
    positive_scene = prompt.replace(EXCLUSIONS_WITH_PEOPLE, "").lower()

    for term in _BANNED_EVERYWHERE + _BANNED_WITH_PEOPLE:
        pattern = rf"\b{re.escape(term)}"
        assert not re.search(pattern, positive_scene), f"{term!r} reached the prompt"
        assert re.search(pattern, negative.lower()), (
            f"{term!r} missing from the negative prompt"
        )


def test_only_an_activity_opens_the_people_path() -> None:
    """The gate is the motif, not a global setting.

    A body beside a jar, a bowl or a tube can only be read as the result of
    taking what is in it — which is the efficacy claim. Nothing in frame is
    doing anything, so there is nothing else for the person to be depicting.
    """
    for product in _OBJECT_PRODUCTS:
        prompt, negative = build_prompt(product)
        assert EXCLUSIONS in prompt, f"{product} should be a still life"
        assert "person" in negative

    for product in _PEOPLE_PRODUCTS:
        prompt, _ = build_prompt(product)
        assert EXCLUSIONS_WITH_PEOPLE in prompt, f"{product} should allow a person"


def test_a_person_needs_a_confident_signal_not_a_topic_word() -> None:
    """Topics name outcomes, and the outcome words are protocol words.

    "magnesium for sleep" matches ``sleep``. Inferring the motif from the topic
    and then drawing a person would put a body in a supplement article — the
    exact picture the whole module refuses. So only a match against `product`
    itself opens the gate; the fallback tier draws objects, which is always
    safe.
    """
    from app.imagery.prompt import _MOTIFS

    prompt, negative = build_prompt("!!!", fallback="magnesium for sleep")

    assert any(m in prompt for m in _MOTIFS[Motif.PROTOCOL]), (
        "still the protocol motif"
    )
    assert EXCLUSIONS in prompt, "but drawn as objects, with no body in frame"
    assert "person" in negative


@pytest.mark.parametrize("product", _OBJECT_PRODUCTS + _PEOPLE_PRODUCTS)
def test_the_exclusions_ride_in_the_positive_prompt(product: str) -> None:
    """FLUX.1-schnell is guidance-distilled, so the negative channel is a no-op.

    The positive prompt is what actually enforces the content rules. If someone
    "tidies up" by moving the exclusions into ``negative_prompt`` only, this
    fails — which is the entire point of asserting it.
    """
    prompt, _ = build_prompt(product)
    assert EXCLUSIONS in prompt or EXCLUSIONS_WITH_PEOPLE in prompt
    assert TREATMENT in prompt


def test_the_verdict_and_the_headline_never_reach_the_prompt() -> None:
    """An illustration must not depict a conclusion a human has not approved yet.

    ``build_prompt`` takes neither, and this test exists to fail loudly if a
    future signature adds one back as an "improvement".
    """
    import inspect

    parameters = set(inspect.signature(build_prompt).parameters)
    assert "headline" not in parameters
    assert "verdict" not in parameters

    prompt, _ = build_prompt("Ashwagandha KSM-66", fallback="ashwagandha for stress")
    for word in ("weak", "supported", "mixed", "no evidence", "cortisol"):
        assert word not in prompt.lower()


def test_a_category_that_settles_the_objects_beats_the_inferred_one() -> None:
    """Regenerate passes a reviewer's category; where it names the props, it wins.

    Asserted against the motif's whole tuple rather than one sentence: the
    property under test is *which motif was chosen*, and pinning a single
    phrasing would make adding a motif variant look like a regression.
    """
    from app.imagery.prompt import _MOTIFS, _PEOPLE_MOTIFS

    inferred, _ = build_prompt("Cold plunge")           # -> protocol, by keyword
    explicit, _ = build_prompt("Cold plunge", subject=Subject.NUTRITION)

    assert any(m in inferred for m in _PEOPLE_MOTIFS[Motif.PROTOCOL])
    assert any(m in explicit for m in _MOTIFS[Motif.FOOD])


@pytest.mark.parametrize(
    "subject", [s for s in Subject if s not in (Subject.SUPPLEMENTS, Subject.NUTRITION)]
)
def test_an_area_category_leaves_the_motif_to_the_product(subject: Subject) -> None:
    """Categories like Fitness or Sleep and recovery span motifs.

    A sleep article may be about melatonin or a bedtime routine, so these
    categories must not pick the props — the product still does. Nor may any
    of them open the people path by itself: a jar of creatine filed under
    Fitness is still a still life.
    """
    for product in ("Creatine monohydrate", "Hot yoga"):
        assert build_prompt(product, subject=subject, seed=7) == build_prompt(
            product, seed=7
        )
        assert build_alt_text(product, subject=subject) == build_alt_text(product)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Creatine monohydrate", Motif.SUPPLEMENT),
        ("hot yoga", Motif.PROTOCOL),
        # "panel" is a device word and is checked before "therapy" is reached.
        ("red light therapy panel", Motif.DEVICE),
        ("retinol cream", Motif.TOPICAL),
        ("lemon juice", Motif.FOOD),
        ("a thing nobody has heard of", None),
    ],
)
def test_the_motif_is_inferred_from_text_when_no_one_has_classified_it(
    text: str, expected: Motif | None
) -> None:
    """Picks which objects to photograph. Never a classification — see _MOTIF_HINTS.

    `subject` is reviewer-set and stays that way; this only chooses a still
    life, is never shown to a reader, and is never written to the column.
    """
    assert infer_motif_subject(text) is expected


@pytest.mark.parametrize(
    ("product", "topic", "expected"),
    [
        # All four resolved to `protocol` — a towel, a glass of water and a
        # timer for a supplement article — while the hints were searched over
        # product, topic and ingredients concatenated. Topics name the
        # *outcome*, and the outcome words are protocol words.
        ("Magnesium glycinate", "magnesium for sleep quality", Motif.SUPPLEMENT),
        ("Melatonin", "melatonin for sleep", Motif.SUPPLEMENT),
        ("Whey protein", "protein for muscle after exercise", Motif.SUPPLEMENT),
        ("Electrolyte powder", "water intake and hydration", Motif.SUPPLEMENT),
        # And the tiering must not break the cases that were already right.
        ("Cold plunge", "cold water immersion", Motif.PROTOCOL),
        ("Red light panel", "red light therapy for skin", Motif.DEVICE),
        # Only when the product says nothing does the topic get a vote.
        ("!!!", "magnesium for sleep", Motif.PROTOCOL),
    ],
)
def test_the_product_outranks_the_topic_when_choosing_a_motif(
    product: str, topic: str, expected: Motif
) -> None:
    """Precedence is by field first — see ``_MOTIF_HINTS``.

    Within-list order cannot fix what a joined haystack breaks: ``SUPPLEMENT``
    is last and ``PROTOCOL`` holds ``sleep``, ``exercise`` and ``training``, so
    a low-precision hint in an early list beat a high-precision hint in a late
    one. Since topics routinely name an outcome, that was the common path for
    supplements rather than an edge case.
    """
    assert infer_motif_subject(product, topic) is expected


def test_an_unrecognised_subject_still_gets_objects_of_its_own() -> None:
    """The old default named no objects and drew pills for everything.

    Checked at every motif variant, because the fallback is four templates now
    and one of them silently losing the noun is exactly the regression that
    would bring the pills back.
    """
    from app.imagery.prompt import _MOTIF_RADIX, _MOTIF_STRIDE

    for variant in range(_MOTIF_RADIX):
        prompt, _ = build_prompt(
            "a thing nobody has heard of", seed=variant * _MOTIF_STRIDE
        )
        assert "a thing nobody has heard of" in prompt


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ashwagandha KSM-66", "Ashwagandha KSM-66"),
        ("  omega-3  ", "omega-3"),
        # A second clause is how a free-text product would steer the render.
        ("creatine, photo of a smiling doctor", "creatine photo of a smiling doctor"),
        ("vitamin D3 / K2", "vitamin D3 K2"),
        ("!!!", ""),
    ],
)
def test_the_subject_noun_is_scrubbed_to_a_plain_phrase(raw: str, expected: str) -> None:
    assert scrub_subject(raw) == expected


def test_a_product_that_scrubs_away_falls_back_to_the_topic() -> None:
    prompt, _ = build_prompt("!!!", fallback="magnesium for sleep")
    assert "magnesium for sleep" in prompt


def test_the_alt_text_says_the_picture_is_generated_and_decorative() -> None:
    """A reader who cannot see it has no other way to know it depicts no finding."""
    alt = build_alt_text("Ashwagandha KSM-66", "ashwagandha for stress")
    assert "Ashwagandha KSM-66" in alt
    assert "generated" in alt.lower()
    assert "no research finding" in alt.lower()


def test_the_negative_prompt_is_sent_even_though_it_is_near_a_no_op() -> None:
    """Belt and braces for a future model or guidance setting where it bites."""
    _, negative = build_prompt("Creatine")
    assert negative == NEGATIVE_PROMPT
    assert "person" in negative


# --- the shared seam --------------------------------------------------------


async def test_the_console_and_the_pipeline_share_one_generator(
) -> None:
    """`generate_illustration` is what both call; a second copy would drift."""
    client = _FakeImageClient()
    illustration = await generate_illustration(
        client,
        product="Creatine monohydrate",
        topic="creatine for strength",
        subject=Subject.SUPPLEMENTS,
        lead_size=LEAD,
        cover_size=COVER,
        session=FakeMediaSession(),
        max_bytes=8 * 1024 * 1024,
        seed=1234,
    )

    assert illustration.lead.seed == illustration.cover.seed == 1234
    assert illustration.lead.alt == illustration.cover.alt
    from app.imagery.prompt import _MOTIFS

    assert illustration.lead.prompt.startswith(
        "Still life of Creatine monohydrate:"
    ), "the noun leads the sentence"
    assert any(
        m in illustration.lead.prompt for m in _MOTIFS[Motif.SUPPLEMENT]
    ), "a classified article should get the supplement motif"
    assert illustration.cover.prompt == illustration.lead.prompt, (
        "drawn together means drawn from one prompt"
    )


# --- where the stage sits ---------------------------------------------------


def test_persist_is_still_the_last_stage() -> None:
    """A structural guard, in the spirit of ``test_publish_path.py``.

    PERSIST's write commits together with the run's completion row, which is
    what stops a killed worker leaving an article whose run still says
    ``running`` — to be requeued as stale and written a second time. Any stage
    added after it silently reopens that window.
    """
    assert list(StageName)[-1] is StageName.PERSIST


def test_illustrate_runs_after_synthesis_and_before_persist() -> None:
    """It illustrates the draft, so it cannot precede the stage that writes one."""
    order = list(StageName)
    assert order.index(StageName.SYNTHESIZE) < order.index(StageName.ILLUSTRATE)
    assert order.index(StageName.ILLUSTRATE) < order.index(StageName.PERSIST)


# --- the spend budget on regenerate -----------------------------------------


def test_the_spend_budget_blocks_after_its_allowance_and_then_expires() -> None:
    """Regenerate is the only console action that bills a provider per press."""
    from datetime import timedelta

    from app.security.spend_throttle import Budget, InMemorySpendThrottle

    now = [1000.0]
    throttle = InMemorySpendThrottle(
        Budget(max_events=3, window=timedelta(seconds=60)), clock=lambda: now[0]
    )

    for _ in range(3):
        assert throttle.retry_after("reviewer-1") is None
        throttle.record("reviewer-1")

    wait = throttle.retry_after("reviewer-1")
    assert wait is not None and 0 < wait <= 60
    # A second reviewer has their own budget: the credit is per account, but a
    # shared office address must not make two reviewers halve each other's.
    assert throttle.retry_after("reviewer-2") is None

    now[0] += 61
    assert throttle.retry_after("reviewer-1") is None


def test_the_budget_counts_renders_so_one_frame_costs_half_of_two() -> None:
    """The unit is the thing being billed, not the button being pressed.

    A one-frame redraw draws one picture. Charging it as a whole press would
    make the cheaper action cost the same as the expensive one, which is the
    wrong incentive on the one control that exists to stop needless spending.
    """
    from datetime import timedelta

    from app.security.spend_throttle import Budget, InMemorySpendThrottle

    now = [1000.0]
    throttle = InMemorySpendThrottle(
        Budget(max_events=4, window=timedelta(seconds=60)), clock=lambda: now[0]
    )

    throttle.record("reviewer-1", 2)
    throttle.record("reviewer-1", 1)
    assert throttle.retry_after("reviewer-1") is None, "three of four renders spent"

    throttle.record("reviewer-1", 1)
    assert throttle.retry_after("reviewer-1") is not None


# --- the dynamic half of the prompt -----------------------------------------
#
# The first version of this module locked everything but the subject noun —
# three words in eighty — and produced a feed where every article was white
# pills on warm beige. These pin the fix: treatment fixed, composition free.


def test_the_seed_moves_the_composition() -> None:
    """Different seeds must compose differently, or the feed is one photograph.

    This is the axis the *provider's* seed cannot supply — it ignores seeding
    entirely, and those identical-looking renders already came from different
    noise. Variety has to be in the prompt.
    """
    prompts = {build_prompt("Creatine", seed=s)[0] for s in range(40)}

    assert len(prompts) > 15, "the seed barely moves the prompt"


def test_the_axes_do_not_advance_in_lockstep() -> None:
    """Mixed radix, so 500 combinations do not collapse to five."""
    from app.imagery.prompt import _ARRANGEMENTS, _FRAMINGS, _LIGHTS, _SURFACES

    seen = {
        tuple(
            next(o for o in axis if o in build_prompt("Creatine", seed=s)[0])
            for axis in (_ARRANGEMENTS, _FRAMINGS, _LIGHTS, _SURFACES)
        )
        for s in range(200)
    }
    # Four axes moving together would give max(len(axis)) == 5 combinations.
    assert len(seen) > 50


def test_the_axes_are_exactly_uniform_over_one_period() -> None:
    """The strides are a mixed radix, and that is a stronger claim than "varies".

    The old strides were odd and coprime — ``(1, 7, 53, 401)`` — on the theory
    that coprimality between the strides buys independence between the axes. It
    does not. With stride 7 against a 5-option axis, ``seed = 35q + r`` gives
    ``(seed % 5, seed // 7 % 5) == (r % 5, r // 7)``: 35 values of ``r`` landing
    on 25 pairs, so 10 framing×arrangement combinations came up exactly twice as
    often as the other 15 (measured over 400k random seeds: chi-square 49073 on
    16 degrees of freedom).

    What matters is each stride against the *product of the preceding axis
    lengths*. Under that radix the distribution is not merely close to uniform,
    it is exact — which is why this asserts equality rather than a bound.
    """
    from app.imagery.prompt import _AXIS_RADIX, _OBJECTS, _PERIOD, _WITH_PEOPLE

    period = math.prod(_AXIS_RADIX)

    for path in (_OBJECTS, _WITH_PEOPLE):
        picks = [
            tuple(
                next(o for o in axis if o in path.compose("scene", seed))
                for axis in path.axes
            )
            for seed in range(period)
        ]
        assert len(set(picks)) == period, "every combination exactly once"

        for index, axis in enumerate(path.axes):
            counts = Counter(pick[index] for pick in picks)
            assert set(counts) == set(axis), "an option that never comes up"
            assert len(set(counts.values())) == 1, f"axis {index} is not uniform"

    # The full period includes the motif digit, which sits past all four of
    # these axes — so it is _PERIOD, not the composition period, that repeats.
    assert build_prompt("Creatine", seed=0) == build_prompt("Creatine", seed=_PERIOD)


def test_both_paths_offer_the_same_number_of_compositions() -> None:
    """The mixed radix is shared, so a path with a shorter axis would skew it.

    Checked at import too — this is here so the failure names the reason.
    """
    from app.imagery.prompt import _AXIS_RADIX, _OBJECTS, _WITH_PEOPLE

    for path in (_OBJECTS, _WITH_PEOPLE):
        assert tuple(len(axis) for axis in path.axes) == _AXIS_RADIX


def test_one_seed_gives_one_prompt_so_both_frames_are_one_shoot() -> None:
    """The lead and the cover must not be composed differently from each other."""
    assert build_prompt("Creatine", seed=99) == build_prompt("Creatine", seed=99)


def test_the_treatment_never_varies() -> None:
    """What holds a feed together is the palette, not the camera angle."""
    for seed in range(30):
        prompt, _ = build_prompt("Creatine", seed=seed)
        assert TREATMENT in prompt
        assert EXCLUSIONS in prompt


@pytest.mark.parametrize("seed", [0, 1, 5, 25, 100, 12345, 2**31])
def test_every_variant_still_refuses_a_person(seed: int) -> None:
    """Varying the composition must not open a door the exclusions closed."""
    for product in _OBJECT_PRODUCTS:
        prompt, _ = build_prompt(product, seed=seed)
        scene = prompt.replace(EXCLUSIONS, "").lower()
        for term in _BANNED_IN_PROMPT:
            assert not re.search(rf"\b{re.escape(term)}", scene), (
                f"{term!r} reached the prompt at seed {seed}"
            )


@pytest.mark.parametrize("seed", [0, 1, 5, 25, 100, 12345, 2**31])
def test_every_people_variant_stays_mid_activity(seed: int) -> None:
    """No pose may drift into the posed-body register, at any seed.

    ``_POSES`` is the axis that could quietly reintroduce the claim — a pose
    that displays rather than acts is an advertisement, which is the efficacy
    claim wearing a different coat.
    """
    for product in _PEOPLE_PRODUCTS:
        prompt, _ = build_prompt(product, seed=seed)
        scene = prompt.replace(EXCLUSIONS_WITH_PEOPLE, "").lower()
        for term in _BANNED_EVERYWHERE + _BANNED_WITH_PEOPLE:
            assert not re.search(rf"\b{re.escape(term)}", scene), (
                f"{term!r} reached the prompt at seed {seed}"
            )
        assert TREATMENT in prompt, (
            "the people path shares the locked palette, or it becomes stock "
            "fitness photography"
        )


def test_different_articles_do_not_share_a_composition_by_default() -> None:
    """The bug this whole axis exists to fix, stated as a test."""
    topics = [
        ("Creatine monohydrate", "creatine for strength"),
        ("hot yoga", "is hot yoga good long term"),
        ("lemon juice", "lemon juice on an empty stomach"),
        ("retinol cream", "retinol for wrinkles"),
        ("red light panel", "red light therapy"),
    ]
    prompts = {
        build_prompt(p, fallback=t, seed=seed_for_run(t))[0] for p, t in topics
    }
    assert len(prompts) == len(topics)


def test_the_seed_changes_what_is_in_the_frame_not_only_the_camera() -> None:
    """The bug this change fixes, stated as a test.

    Every subject held one motif string, so ``SUPPLEMENT`` — which is most of a
    wellness feed's vocabulary — always drew the same jar of capsules, and the
    four composition axes could only re-photograph it. Five hundred angles on
    one still life read as one picture.
    """
    from app.imagery.prompt import _MOTIF_RADIX, _MOTIF_STRIDE, _MOTIFS, _PERIOD

    drawn = {
        next(m for m in _MOTIFS[Motif.SUPPLEMENT] if m in prompt)
        for prompt in (
            build_prompt("Creatine", seed=s)[0]
            for s in range(0, _PERIOD, _MOTIF_STRIDE)
        )
    }
    assert len(drawn) == _MOTIF_RADIX, "the seed never changes the objects"


def _motif_and_camera(
    product: str, motifs: tuple[str, ...], camera: tuple[str, ...]
) -> Counter[tuple[str, str]]:
    """Which objects and which angle each seed of one whole period selects."""
    from app.imagery.prompt import _PERIOD

    return Counter(
        (
            next(m for m in motifs if m in text),
            next(f for f in camera if f in text),
        )
        for text in (build_prompt(product, seed=s)[0] for s in range(_PERIOD))
    )


def test_the_motif_digit_is_uniform_and_independent_of_the_camera() -> None:
    """The motif is a fifth digit on the same radix, and inherits its claim.

    ``test_the_axes_are_exactly_uniform_over_one_period`` explains why this is
    an equality rather than a bound: the old coprime strides looked uniform and
    were not, and the failure was arithmetic. A digit whose stride is not the
    product of everything before it correlates with the axes it straddles —
    here that would mean certain objects only ever being shot from certain
    angles, which is the sameness this axis exists to remove.
    """
    from app.imagery.prompt import (
        _FRAMINGS,
        _MOTIF_RADIX,
        _MOTIF_STRIDE,
        _MOTIFS,
        _PEOPLE_FRAMINGS,
        _PEOPLE_MOTIFS,
        _POSES,
    )

    for product, motifs, camera in (
        ("Creatine", _MOTIFS[Motif.SUPPLEMENT], _FRAMINGS),
        ("Hot yoga", _PEOPLE_MOTIFS[Motif.PROTOCOL], _PEOPLE_FRAMINGS),
    ):
        pairs = _motif_and_camera(product, motifs, camera)

        by_motif = Counter[str]()
        for (motif, _), count in pairs.items():
            by_motif[motif] += count
        assert set(by_motif) == set(motifs), f"{product}: a motif never comes up"
        assert set(by_motif.values()) == {_MOTIF_STRIDE}, (
            f"{product}: the motif digit is not uniform"
        )

        assert len(pairs) == _MOTIF_RADIX * len(camera), (
            f"{product}: not every objects x angle combination occurs"
        )
        assert len(set(pairs.values())) == 1, (
            f"{product}: the motif tracks the camera instead of varying freely"
        )

    # A pose is the people path's arrangement axis, so it must be free of the
    # motif too — the objects a person is photographed among cannot decide what
    # the person is doing.
    poses = _motif_and_camera("Hot yoga", _PEOPLE_MOTIFS[Motif.PROTOCOL], _POSES)
    assert len(set(poses.values())) == 1


def test_no_option_in_any_vocabulary_names_a_claim_shaped_thing() -> None:
    """Exhaustive over the words, not sampled over seeds.

    The seeded content-safety tests above check seven seeds out of 5,184, which
    was adequate at 500 combinations and is not now — a banned word added to one
    motif variant would be missed by every sampled seed. Checking the
    vocabularies directly is both cheaper and a stronger guarantee: it fails
    when the word is *written*, not when a seed happens to select it.
    """
    from app.imagery import prompt as prompt_module

    object_words = (
        *(v for variants in prompt_module._MOTIFS.values() for v in variants),
        *prompt_module._DEFAULT_MOTIFS,
        *prompt_module._DEFAULT_MOTIFS_UNNAMED,
        *(v for _, variants in prompt_module._OUTCOME_SCENES for v in variants),
        *prompt_module._FRAMINGS,
        *prompt_module._ARRANGEMENTS,
        *prompt_module._SURFACES,
        *prompt_module._LIGHTS,
    )
    for option in object_words:
        for term in _BANNED_IN_PROMPT:
            assert not re.search(rf"\b{re.escape(term)}", option.lower()), (
                f"{term!r} is written into the still-life vocabulary: {option!r}"
            )

    people_words = (
        *(
            v
            for variants in prompt_module._PEOPLE_MOTIFS.values()
            for v in variants
        ),
        *prompt_module._PEOPLE_FRAMINGS,
        *prompt_module._POSES,
        *prompt_module._PEOPLE_SETTINGS,
        *prompt_module._LIGHTS,
    )
    for option in people_words:
        for term in _BANNED_EVERYWHERE + _BANNED_WITH_PEOPLE:
            assert not re.search(rf"\b{re.escape(term)}", option.lower()), (
                f"{term!r} is written into the people vocabulary: {option!r}"
            )


# --- redrawing one frame ----------------------------------------------------
#
# A reviewer can like the picture in the prose and not the one on the tile.
# Making them replace both to fix one costs a render nobody wanted and takes
# away a picture that was fine, so either frame can be drawn alone. What has to
# stay true when it is: the other frame is untouched, exactly one render is
# billed, and the record of how each picture was made still describes that
# picture rather than its neighbour.


async def _pair(client: _FakeImageClient, seed: int = 1) -> Illustration:
    return await generate_illustration(
        client,
        product="Creatine monohydrate",
        topic="creatine for strength",
        subject=Subject.SUPPLEMENTS,
        lead_size=LEAD,
        cover_size=COVER,
        session=FakeMediaSession(),
        max_bytes=8 * 1024 * 1024,
        seed=seed,
    )


async def test_redrawing_the_tile_leaves_the_article_picture_alone(
) -> None:
    client = _FakeImageClient()
    before = await _pair(client)
    drawn_for_the_pair = len(client.requests)

    after = await generate_illustration(
        client,
        product="Creatine monohydrate",
        topic="creatine for strength",
        subject=Subject.SUPPLEMENTS,
        lead_size=LEAD,
        cover_size=COVER,
        session=FakeMediaSession(),
        max_bytes=8 * 1024 * 1024,
        seed=99,
        frames=[ImageFrame.COVER],
        keep=before,
    )

    assert after.lead == before.lead, "the frame nobody asked about must not move"
    assert after.cover.src != before.cover.src
    assert len(client.requests) == drawn_for_the_pair + 1, "one frame, one render"
    assert client.requests[-1].width == COVER[0]


async def test_redrawing_the_article_picture_leaves_the_tile_alone(
) -> None:
    """The direction that matters most: it is the one that moves the pairing.

    A new lead changes ``lead.src``, which is the value ``derive_card`` compares
    against the document. The cover surviving unchanged is what makes the
    console's job — swap the editor's image node to match — a repair rather
    than a race.
    """
    client = _FakeImageClient()
    before = await _pair(client)

    after = await generate_illustration(
        client,
        product="Creatine monohydrate",
        topic="creatine for strength",
        subject=Subject.SUPPLEMENTS,
        lead_size=LEAD,
        cover_size=COVER,
        session=FakeMediaSession(),
        max_bytes=8 * 1024 * 1024,
        seed=99,
        frames=[ImageFrame.LEAD],
        keep=before,
    )

    assert after.cover == before.cover
    assert after.lead.src != before.lead.src
    assert client.requests[-1].width == LEAD[0]


async def test_each_frame_records_the_prompt_it_was_actually_drawn_under(
) -> None:
    """Why provenance moved off the pair and onto the frames.

    After a one-frame redraw the two pictures come from two different prompts.
    A single shared field would have to be a true record of one and a false
    record of the other — and it would still answer when asked, which is worse
    than not knowing.
    """
    client = _FakeImageClient()
    before = await _pair(client, seed=1)

    after = await generate_illustration(
        client,
        product="Creatine monohydrate",
        topic="creatine for strength",
        subject=Subject.SUPPLEMENTS,
        lead_size=LEAD,
        cover_size=COVER,
        session=FakeMediaSession(),
        max_bytes=8 * 1024 * 1024,
        seed=seed_for_run("a composition away from seed 1"),
        frames=[ImageFrame.COVER],
        keep=before,
    )

    assert after.lead.prompt == before.lead.prompt
    assert after.cover.prompt != after.lead.prompt
    assert after.cover.seed != after.lead.seed
    assert after.lead.model and after.cover.model


async def test_a_partial_redraw_needs_something_to_keep() -> None:
    """Half an illustration is not representable, and must not be inventable.

    A cover with no lead is a tile picture with nothing in the article to pair
    it against — the one state ``derive_card`` cannot reason about. Refused
    here as a caller mistake rather than an ``ImageError``, because the console
    answers it with a 409 and a sentence, not with "the provider failed".
    """
    client = _FakeImageClient()
    with pytest.raises(ValueError, match="no existing illustration"):
        await generate_illustration(
            client,
            product="Creatine monohydrate",
            lead_size=LEAD,
            cover_size=COVER,
            session=FakeMediaSession(),
            max_bytes=8 * 1024 * 1024,
            seed=5,
            frames=[ImageFrame.COVER],
        )
    assert client.requests == [], "nothing may be billed for a refused call"


async def test_asking_for_no_frames_is_refused() -> None:
    client = _FakeImageClient()
    with pytest.raises(ValueError, match="no frames"):
        await generate_illustration(
            client,
            product="Creatine monohydrate",
            lead_size=LEAD,
            cover_size=COVER,
            session=FakeMediaSession(),
            max_bytes=8 * 1024 * 1024,
            seed=5,
            frames=[],
        )
    assert client.requests == []


def test_a_pre_split_row_carries_its_shared_prompt_onto_both_frames() -> None:
    """Rows written before provenance moved must not lose it.

    ``generated_imagery`` is JSONB and there is no migration for a Python-side
    shape change, so the old top-level fields are pushed down on the way in.
    Dropping them instead would silently discard the only record of how two
    published pictures were made, on the first partial redraw.
    """
    legacy = {
        "prompt": "loose capsules on pale linen",
        "negative_prompt": "person, people, human",
        "model": "huggingface/black-forest-labs/FLUX.1-schnell",
        "lead": {
            "src": "/api/media/ab/cd.webp",
            "alt": "a",
            "width": 1216,
            "height": 832,
            "seed": 7,
        },
        "cover": {
            "src": "/api/media/ef/01.webp",
            "alt": "a",
            "width": 864,
            "height": 1152,
            "seed": 7,
        },
    }
    restored = Illustration.model_validate(legacy)

    assert restored.lead.prompt == restored.cover.prompt == legacy["prompt"]
    assert restored.lead.model == legacy["model"]
    assert restored.cover.negative_prompt == legacy["negative_prompt"]
    # And the shared fields are gone from what we write back, so the next
    # partial redraw cannot resurrect a prompt that describes neither picture.
    assert set(restored.model_dump(mode="json")) == {"lead", "cover"}


def test_a_frames_own_provenance_wins_over_a_legacy_shared_field() -> None:
    """Otherwise the backfill would overwrite the thing it exists to preserve."""
    restored = Illustration.model_validate(
        {
            "prompt": "the old shared prompt",
            "lead": {
                "src": "/api/media/ab/cd.webp",
                "alt": "a",
                "width": 1216,
                "height": 832,
                "seed": 7,
                "prompt": "what the lead was really drawn under",
            },
            "cover": {
                "src": "/api/media/ef/01.webp",
                "alt": "a",
                "width": 864,
                "height": 1152,
                "seed": 8,
            },
        }
    )
    assert restored.lead.prompt == "what the lead was really drawn under"
    assert restored.cover.prompt == "the old shared prompt"


def test_an_article_about_an_area_of_health_gets_its_scene_and_never_its_claim() -> None:
    """2026-10-23: "Back pain" fell through to "objects associated with back
    pain", and FLUX drew pharmacy bottles with garbled labels."""
    from app.imagery.prompt import _OUTCOME_SCENES

    prompt, _ = build_prompt("Back pain", fallback="Back pain", outcome="reduces back pain")

    scene = prompt.replace(EXCLUSIONS, "").lower()
    assert "reduces" not in scene and "pain" not in scene
    assert any(
        variant.lower() in scene
        for hints, variants in _OUTCOME_SCENES
        if "back" in hints
        for variant in variants
    )


def test_a_product_that_names_a_motif_still_wins_over_the_outcome() -> None:
    prompt, _ = build_prompt("Magnesium glycinate", outcome="improves sleep quality")
    assert prompt.startswith("Still life of Magnesium glycinate")
