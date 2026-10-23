"""The review queue: reading, editing, illustrating, approving and rejecting drafts."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError

from app.api.console.schemas import (
    ArticleDetailOut,
    CardPreviewOut,
    GeneratedFrameOut,
    IllustrationOut,
    QueuePageOut,
    RegenerateIllustrationRequest,
    RejectRequest,
    SaveContentRequest,
    SetSubjectRequest,
)
from app.api.deps import (
    IdPath,
    IllustrationThrottleDep,
    ReviewerDep,
    SessionDep,
    SettingsDep,
    require_reviewer,
)
from app.domain.contracts import Illustration
from app.domain.enums import (
    ArticleStatus,
)
from app.imagery.base import ImageError
from app.imagery.factory import build_image_client
from app.services.illustration import BOTH_FRAMES, fresh_seed, generate_illustration
from app.services.review import ReviewError, ReviewService

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_reviewer)], tags=["console"])


@router.get("/articles", response_model=QueuePageOut)
async def list_queue(
    session: SessionDep,
    status_filter: Annotated[ArticleStatus, Query(alias="status")] = (
        ArticleStatus.PENDING_REVIEW
    ),
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> QueuePageOut:
    """The review queue, newest first.

    ``status`` is a parameter so the console can also show the
    ``validation_failed`` tab — those drafts are never approvable, but they are
    the system's prompt-bug feedback loop, and hiding them makes invariant #2
    enforcement invisible.
    """
    try:
        items, next_cursor = await ReviewService(session).queue(status_filter, cursor, limit)
    except ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return QueuePageOut(items=items, next_cursor=next_cursor)


@router.get("/articles/{article_id}", response_model=ArticleDetailOut)
async def get_article_detail(article_id: IdPath, session: SessionDep) -> ArticleDetailOut:
    """Draft, sources and validation report in one request.

    One request rather than three because the review screen renders the editor
    and the sources panel together — the "fast source access" requirement is
    about the reviewer's time, and a waterfall of requests spends it.
    """
    try:
        detail = await ReviewService(session).article_detail(article_id)
    except ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ArticleDetailOut(**detail)


@router.patch("/articles/{article_id}/content", status_code=status.HTTP_204_NO_CONTENT)
async def save_content(
    article_id: IdPath,
    payload: SaveContentRequest,
    session: SessionDep,
    reviewer: ReviewerDep,
) -> None:
    """Autosave editor content into ``edited_content``.

    Debounced ~800ms client-side, so this must be cheap and idempotent. Writes
    ``edited_content`` only; ``original_content`` is immutable and the DB
    trigger enforces that independently of this handler.
    """
    try:
        await ReviewService(session).save_edits(article_id, payload.content, reviewer.id)
    except ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.patch("/articles/{article_id}/subject", status_code=status.HTTP_204_NO_CONTENT)
async def set_subject(
    article_id: IdPath,
    payload: SetSubjectRequest,
    session: SessionDep,
    reviewer: ReviewerDep,
) -> None:
    """Classify what kind of thing the article assesses.

    Separate from the content PATCH, and not part of approve, because it is the
    one field a reviewer may legitimately change *after* publication: it drives
    a colour and a browse listing rather than a word the reader was shown, and
    getting it wrong should be correctable without touching the article.
    """
    try:
        await ReviewService(session).set_subject(article_id, payload.subject, reviewer.id)
    except ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/articles/{article_id}/approve", status_code=status.HTTP_204_NO_CONTENT)
async def approve(article_id: IdPath, session: SessionDep, reviewer: ReviewerDep) -> None:
    """Publish. The only path to ``published`` in the entire system.

    409 when the article isn't in ``pending_review``, or when editing has
    orphaned a citation. The detail names the specific handles — a reviewer
    needs to know which citation broke, not merely that one did.
    """
    try:
        await ReviewService(session).approve(article_id, reviewer.id)
    except ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/articles/{article_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject(
    article_id: IdPath,
    payload: RejectRequest,
    session: SessionDep,
    reviewer: ReviewerDep,
) -> None:
    """Reject with a required reason. Terminal.

    Reachable from ``pending_review`` and from ``validation_failed``. The
    latter is the console's discard action: with no DELETE on articles this is
    the only way a draft leaves a queue tab, and a failed draft with no
    available action simply accumulates.
    """
    try:
        await ReviewService(session).reject(article_id, reviewer.id, payload.reason)
    except ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/articles/{article_id}/card", response_model=CardPreviewOut)
async def get_card_preview(article_id: IdPath, session: SessionDep) -> CardPreviewOut:
    """The feed tile this draft would publish as. Read-only.

    Derived by the same ``services/card.py::derive_card`` that runs inside
    ``approve()``, from ``COALESCE(edited_content, original_content)`` — so it
    reflects the reviewer's edits as of their last autosave, and the console
    flushes before asking, exactly as it does before approving.

    A separate route from the article detail because the two have opposite
    caching lives: a draft is fetched once per review session, while a tile has
    to be right at the moment somebody looks at it.
    """
    try:
        card = await ReviewService(session).card_preview(article_id)
    except ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return CardPreviewOut(**card)


@router.post("/articles/{article_id}/illustration", response_model=IllustrationOut)
async def regenerate_illustration(
    article_id: IdPath,
    session: SessionDep,
    settings: SettingsDep,
    reviewer: ReviewerDep,
    throttle: IllustrationThrottleDep,
    payload: RegenerateIllustrationRequest | None = None,
) -> IllustrationOut:
    """Draw this article's pictures again, with a new seed.

    ``frames`` picks which — both by default, or just the article's own
    ``lead`` or just the feed tile's ``cover``. The two are independent because
    they are seen in different places and judged separately: a reviewer who
    likes the picture in the prose and not the one on the tile would otherwise
    have to give up the first to fix the second, and pay twice to do it.
    Redrawing one alone is safe for the same reason the pair was never a
    photograph and its crop — both come from the same locked, claim-free prompt
    builder, so neither frame can assert what the other does not.

    The one console action that bills an external provider per press, which is
    why it is the one with a spend budget in front of it. The budget is checked
    **before** the render for the same reason ``login_throttle`` is checked
    before the Argon2 hash: a limit applied after the expensive part protects
    nothing. It counts renders, so a one-frame press costs half a two-frame one.

    Uses the article's ``subject`` when a reviewer has set one, so pressing
    this after classifying a draft genuinely produces a better-composed picture
    than the pipeline could — the pipeline runs before the article row exists
    and has no subject to read.

    **Does not touch the document.** The response carries both frames and the
    console swaps the editor's image node itself, so the new ``src`` reaches
    ``edited_content`` through autosave and its media check. See
    ``ReviewService.set_illustration``.

    Status codes: 409 for a draft that is not ``pending_review`` *or* for a
    one-frame redraw on an article with no imagery to keep the other from, 429
    when the budget is spent, 503 when no image provider is configured, 502
    when the provider failed.
    """
    requested = frozenset(payload.frames) if payload is not None else BOTH_FRAMES
    # Crop mode draws both frames from one render, always: a cover cut from a
    # lead the article does not carry would break the pairing rule. So a
    # one-frame press redraws both, and is billed (and budgeted) as one render.
    cover_from_lead = settings.image_cover_mode == "crop"
    if cover_from_lead:
        requested = BOTH_FRAMES
    renders = 1 if cover_from_lead else len(requested)

    service = ReviewService(session)
    try:
        article = await service.article_for_illustration(article_id)
    except ReviewError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if article.status != ArticleStatus.PENDING_REVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"cannot regenerate imagery for an article in state "
                f"'{article.status}'; only drafts pending review can be changed"
            ),
        )

    # A frame not being redrawn has to come from somewhere, and half an
    # illustration is not a thing this system can hold: a cover with no lead is
    # a tile picture with nothing in the article to pair it against. Refused
    # here rather than quietly upgraded to both, because the upgrade would bill
    # two renders to someone who asked for one.
    keep = _stored_illustration(article.generated_imagery, article_id)
    if keep is None and requested != BOTH_FRAMES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This draft has no generated pictures yet, so there is no other "
                "frame to keep. Regenerate both."
            ),
        )

    if (wait := throttle.retry_after(reviewer.id)) is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Too many image regenerations. Try again in {int(wait) + 1}s — "
                "each render is billed to the project's account."
            ),
            headers={"Retry-After": str(int(wait) + 1)},
        )

    client = build_image_client(settings)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Image generation is off or not configured on this server. "
                "It needs IMAGE_PROVIDER=huggingface and IMAGE_GEN_KEY set to a "
                "Hugging Face token with the 'Make calls to Inference "
                "Providers' permission."
            ),
        )

    try:
        illustration = await generate_illustration(
            client,
            product=article.product,
            topic=article.topic,
            subject=article.subject,
            ingredients=" ".join(article.ingredients or ()),
            outcome=" ".join(article.target_claims or ()),
            lead_size=(settings.image_lead_width, settings.image_lead_height),
            cover_size=(settings.image_cover_width, settings.image_cover_height),
            session=session,
            max_bytes=settings.media_max_bytes,
            seed=fresh_seed(),
            frames=requested,
            keep=keep,
            cover_from_lead=cover_from_lead,
        )
    except ImageError as exc:
        # 502, not 500: the failure is upstream and the reviewer's next useful
        # action is to try again, which a 500 would not suggest. Not counted
        # against the budget either — nothing was billed.
        logger.warning("regenerating imagery for article %s failed: %s", article_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"The image provider did not answer: {exc}",
        ) from exc

    throttle.record(reviewer.id, renders)
    await service.set_illustration(
        article_id, illustration.model_dump(mode="json"), reviewer.id
    )
    return IllustrationOut(
        lead=GeneratedFrameOut(src=illustration.lead.src, alt=illustration.lead.alt),
        cover=GeneratedFrameOut(src=illustration.cover.src, alt=illustration.cover.alt),
        # Sorted so the field is stable across requests; `requested` is a set.
        redrawn=sorted(requested, key=lambda frame: frame.value),
    )


def _stored_illustration(raw: Any, article_id: str) -> Illustration | None:
    """The article's current frames, or ``None`` if it has none we can read.

    A row that fails to parse is treated as no row at all, which costs a
    reviewer nothing worse than being told to redraw both. The alternative —
    letting a ``ValidationError`` out of here — turns a stale JSON shape into a
    500 on a button whose entire job is to replace that JSON.
    """
    if not isinstance(raw, dict):
        return None
    try:
        return Illustration.model_validate(raw)
    except ValidationError:
        logger.warning(
            "article %s has generated_imagery in a shape this build cannot read; "
            "treating it as absent",
            article_id,
        )
        return None
