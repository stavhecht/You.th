"""Draw an article's two pictures and put them in the media store.

**One function, two callers, on purpose.** ``IllustrateStage`` calls this
during a pipeline run and the console's regenerate route calls it when a
reviewer presses the button. A second implementation would drift, and the drift
would be a feed tile that no longer matches the article it belongs to — which
is precisely the failure the pairing rule in ``services/card.py`` exists to
prevent, arriving through the back door.

Two renders, one prompt, one seed: landscape for the prose column, portrait for
the feed tile.

**They are two pictures of the same subject in the same style — not one
photograph at two aspect ratios.** That distinction was measured, not assumed,
and it cost a wrong claim in the docs before it was: the same prompt and the
same seed at two sizes produce visibly different compositions, because the
latent noise a seed initialises has the shape of the frame. Worse, on the
provider this actually routes to (`nscale`, through HF's OpenAI-shaped
`/v1/images/generations`), **the seed is ignored outright** — two renders at
identical prompt, size and seed came back different. See ``seed_for_run``.

So what the pairing rule in ``services/card.py`` guarantees is narrower than
"the tile is a crop of the article's picture", and it is still the thing worth
guaranteeing: the tile's picture came from the same generation, for this
article, under the same locked claim-free prompt, and it is discarded the
moment the article's own picture is no longer the one drawn beside it. Neither
frame asserts anything, so neither can assert something the other does not.

**``IMAGE_COVER_MODE=crop`` (the default since 2026-09-30) makes them one
photograph**: the landscape lead is rendered once and the portrait cover is
centre-cropped out of it with Pillow (``imagery/encode.py::crop_to_ratio``),
which also halves the bill. The still-life style centres its subject in
generous negative space, so the crop is safe. Everything above describes
``render`` mode, which draws the cover as its own generation.

Nothing is written back until every requested frame has been drawn and stored.
A first render that succeeds beside a second that fails leaves the article
exactly as it was — which matters most in the case that looks least dangerous,
a cover drawn against a lead that then failed: a tile picture with nothing in
the article to pair it against is the one state ``derive_card`` cannot reason
about.

**Either frame can be drawn on its own** (``frames=``), keeping the other from
``keep=``. That is a reviewer affordance — liking the article's picture and not
the tile's should not cost two renders to fix one — and it is safe for the same
reason the pair was never a photograph and its crop: both frames come from the
same locked, claim-free prompt builder for the same article, so neither can
assert anything the other does not. What it does mean is that provenance had to
move onto the frames themselves; see ``domain/contracts.py::GeneratedImage``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from collections.abc import Collection, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.contracts import GeneratedImage, Illustration
from app.domain.enums import ImageFrame, Subject
from app.imagery.base import ImageClient, ImageError, ImageRequest, RenderedImage
from app.imagery.encode import crop_to_ratio
from app.imagery.prompt import build_alt_text, build_prompt
from app.services.media import (
    MEDIA_SRC_RE,
    StoredImage,
    UnsupportedMediaError,
    store_image,
)

logger = logging.getLogger(__name__)

#: Ceiling on the seed, so it survives a round trip through JSON and through
#: providers that reject anything wider than 32 bits.
_SEED_MODULUS = 2**32

#: What the pipeline asks for every time, and the console's default.
BOTH_FRAMES: frozenset[ImageFrame] = frozenset(ImageFrame)


def seed_for_run(run_id: str) -> int:
    """A stable seed for one run — *intended* so a retry redraws the same picture.

    The intent: a run that failed retryably at synthesis and succeeded on
    attempt two is the same article, and re-rolling would show a reviewer a
    different picture on their second look for no reason visible to them.

    **Measured 2026-08-24: the image provider does not honour it.** Two renders
    at identical prompt, size and seed came back byte-different through
    `nscale`, which HF's router reaches via an OpenAI-shaped images endpoint
    that has no seed parameter to carry.

    So the seed is sent to the provider on spec and ignored — but it is **not**
    decorative, because `imagery/prompt.py` uses it to choose the prompt's
    arrangement, framing, light and surface. That selection is ours, runs before
    the request, and cannot be ignored by anyone. The practical effect is the
    one originally wanted: a retried run composes the same photograph, and
    Regenerate composes a different one. What a stable seed no longer buys is
    byte-identical output, so a retry still pays for a second file.

    `fal-ai` and `replicate` do honour seeds and are one `IMAGE_INFERENCE_PROVIDER`
    line away, which would restore that last property too.

    Hashed rather than sliced out of the uuid so it keeps working if run ids
    ever stop being uuids.
    """
    return int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:4], "big")


def fresh_seed() -> int:
    """A new seed, for the console's regenerate action.

    The opposite requirement to ``seed_for_run``: pressing regenerate means
    "not that one", so the same seed would make the button appear broken.
    """
    return secrets.randbelow(_SEED_MODULUS)


async def generate_illustration(
    client: ImageClient,
    *,
    product: str,
    topic: str = "",
    subject: Subject | None = None,
    ingredients: str = "",
    outcome: str = "",
    lead_size: tuple[int, int],
    cover_size: tuple[int, int],
    session: AsyncSession,
    max_bytes: int,
    seed: int,
    frames: Collection[ImageFrame] = BOTH_FRAMES,
    keep: Illustration | None = None,
    cover_from_lead: bool = False,
) -> Illustration:
    """Render the requested frames, store them, and describe what was made.

    Args:
        session: where the bytes go — ``media_objects``, via
            ``services/media.py``. **Not committed here.** In the pipeline that
            is the orchestrator's stage commit, and in the console it is the
            request session; either way the images land with the row that
            points at them or not at all, which the disk store could not offer.
        subject: ``None`` on every pipeline call — it is reviewer-set and the
            article does not exist yet. The regenerate route passes the real
            one, which is why pressing it after classifying a draft is worth
            doing. Absent, ``imagery/prompt.py`` infers which objects to
            photograph from the text below; that inference picks a picture and
            is never written to ``articles.subject``.
        ingredients: joined actives, as one more signal for that inference.
        outcome: the joined claims, read for ``imagery/prompt.py``'s outcome
            scenes when the product names no motif; never put in the prompt.
        seed: selects the prompt's arrangement, framing, light and surface.
            Every frame drawn on this call shares it, so they come from one
            shoot; a frame carried over from ``keep`` keeps its own.
        frames: which frames to draw. Both, unless a reviewer asked for one.
        keep: the article's current illustration, whose frames fill in for any
            not being redrawn. Required whenever ``frames`` is partial —
            returning half an ``Illustration`` is not representable, and it
            should not be: the tile's cover is only ever honest next to a lead.
        cover_from_lead: render the lead once and centre-crop the cover out of
            it (``IMAGE_COVER_MODE=crop``). Both frames are then always drawn
            together — a cover cut from a lead that is not the article's own
            would break the pairing rule — so ``frames`` and ``keep`` are
            ignored, and the call costs one render, not two.

    Raises:
        ValueError: a partial redraw with nothing to keep the other frame from.
            A caller mistake rather than a provider failure, so it is not an
            ``ImageError`` — the console checks for it and answers 409, which
            is a different sentence to a reviewer than "the provider failed".
        ImageError: a render failed, a result is too large for the store, or a
            stored path came back in a shape the document checker would refuse.
            Callers in the pipeline treat this as "no picture", never as a
            reason to fail a run.
    """
    wanted = BOTH_FRAMES if cover_from_lead else frozenset(frames)
    if not wanted:
        raise ValueError("generate_illustration was asked to draw no frames")
    if (carried := BOTH_FRAMES - wanted) and keep is None:
        raise ValueError(
            f"asked to redraw only {_names(wanted)}, but there is no existing "
            f"illustration to take the {_names(carried)} frame from"
        )

    # One prompt for every frame drawn on this call — same seed, so they pick
    # the same arrangement, framing, light and surface. The seed does real work
    # here even though the provider ignores the copy it is also sent; see
    # ``seed_for_run``.
    prompt, negative_prompt = build_prompt(
        product,
        subject=subject,
        fallback=topic,
        ingredients=ingredients,
        seed=seed,
        outcome=outcome,
    )
    # Same `subject` as the prompt, so the alt text cannot call a photograph of
    # someone on a mat a still life. It gets no `topic`, matching the people
    # predicate in `build_prompt` — see `prompt.py::_draws_a_person`.
    alt = build_alt_text(product, topic, subject=subject)

    async def draw_or_keep(frame: ImageFrame, size: tuple[int, int]) -> GeneratedImage:
        if frame not in wanted:
            # Guarded above: `keep` cannot be None while any frame is carried.
            assert keep is not None
            return keep.lead if frame is ImageFrame.LEAD else keep.cover
        return await _render_and_store(
            client,
            prompt=prompt,
            negative_prompt=negative_prompt,
            size=size,
            seed=seed,
            alt=alt,
            session=session,
            max_bytes=max_bytes,
            frame=frame,
        )

    if cover_from_lead:
        rendered = await _render(
            client, prompt=prompt, negative_prompt=negative_prompt, size=lead_size, seed=seed
        )
        cropped = await asyncio.to_thread(crop_to_ratio, rendered, cover_size)
        lead = await _store_frame(
            rendered, client=client, prompt=prompt, negative_prompt=negative_prompt,
            seed=seed, alt=alt, session=session, max_bytes=max_bytes, frame=ImageFrame.LEAD,
        )
        cover = await _store_frame(
            cropped, client=client, prompt=prompt, negative_prompt=negative_prompt,
            seed=seed, alt=alt, session=session, max_bytes=max_bytes, frame=ImageFrame.COVER,
        )
        return Illustration(lead=lead, cover=cover)

    # Sequentially, in document order. Two concurrent renders would halve the
    # wait and double the peak spend on a provider that bills per image; the
    # reviewer is looking at a spinner either way.
    lead = await draw_or_keep(ImageFrame.LEAD, lead_size)
    cover = await draw_or_keep(ImageFrame.COVER, cover_size)
    return Illustration(lead=lead, cover=cover)


def _names(frames: Iterable[ImageFrame]) -> str:
    return " and ".join(sorted(frame.value for frame in frames))


async def _render_and_store(
    client: ImageClient,
    *,
    prompt: str,
    negative_prompt: str,
    size: tuple[int, int],
    seed: int,
    alt: str,
    session: AsyncSession,
    max_bytes: int,
    frame: ImageFrame,
) -> GeneratedImage:
    rendered = await _render(
        client, prompt=prompt, negative_prompt=negative_prompt, size=size, seed=seed
    )
    return await _store_frame(
        rendered,
        client=client,
        prompt=prompt,
        negative_prompt=negative_prompt,
        seed=seed,
        alt=alt,
        session=session,
        max_bytes=max_bytes,
        frame=frame,
    )


async def _render(
    client: ImageClient,
    *,
    prompt: str,
    negative_prompt: str,
    size: tuple[int, int],
    seed: int,
) -> RenderedImage:
    width, height = size
    return await client.render(
        ImageRequest(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            seed=seed,
        )
    )


async def _store_frame(
    rendered: RenderedImage,
    *,
    client: ImageClient,
    prompt: str,
    negative_prompt: str,
    seed: int,
    alt: str,
    session: AsyncSession,
    max_bytes: int,
    frame: ImageFrame,
) -> GeneratedImage:
    _assert_fits(rendered, max_bytes=max_bytes, frame=frame)
    stored = await _store(rendered, session=session, frame=frame)

    logger.info(
        "illustration %s: %dx%d, %d bytes, seed %d -> %s",
        frame.value,
        rendered.width,
        rendered.height,
        len(rendered.data),
        seed,
        stored.src,
    )
    return GeneratedImage(
        src=stored.src,
        alt=alt,
        width=rendered.width,
        height=rendered.height,
        seed=rendered.seed,
        # Recorded per frame, not per pair: after a partial redraw these two
        # genuinely differ, and a shared field would have to be wrong about one
        # of them. See ``domain/contracts.py::GeneratedImage``.
        prompt=prompt,
        negative_prompt=negative_prompt,
        model=client.model_id,
    )


def _assert_fits(rendered: RenderedImage, *, max_bytes: int, frame: ImageFrame) -> None:
    """The size gate the store itself does not have.

    ``store_image`` writes whatever it is handed; the ceiling lives on the
    console's *upload* route, which a generated image never passes through. So
    this is the only thing standing between a provider returning something
    enormous and it going straight into the directory every feed render is
    served from.
    """
    if len(rendered.data) > max_bytes:
        raise ImageError(
            f"the {frame.value} render is {len(rendered.data)} bytes, over the "
            f"{max_bytes}-byte store limit"
        )


async def _store(
    rendered: RenderedImage, *, session: AsyncSession, frame: ImageFrame
) -> StoredImage:
    """Store the bytes and check the path is one a document may hold.

    Both failures here mean a bug rather than a bad provider response, and both
    are converted to ``ImageError`` rather than raised: a broken encoder should
    cost a picture, not a run. They are worth checking anyway, because either
    one produces an article whose *first autosave* is refused by
    ``assert_media_is_ours`` — with an error message aimed at a paste the
    reviewer never made.
    """
    try:
        stored = await store_image(rendered.data, session=session)
    except UnsupportedMediaError as exc:
        raise ImageError(
            f"the {frame.value} render is not a format the media store accepts: {exc}"
        ) from exc

    if not MEDIA_SRC_RE.match(stored.src):
        raise ImageError(
            f"the {frame.value} render stored to {stored.src!r}, which is not a path a "
            "document may reference"
        )
    return stored
