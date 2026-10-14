"""Editorial console endpoints. JWT required on every route.

Auth is applied at the router level (``dependencies=[Depends(require_reviewer)]``)
rather than per-handler. A per-handler decorator is one forgotten line away
from an unauthenticated console endpoint; a router-level dependency cannot be
forgotten by adding a route.

Two deliberate omissions from this surface:

* **No DELETE on articles.** Rejection is a state, and the audit trail is the
  product.
* **No endpoint that sets status directly.** ``approve`` and ``reject`` are the
  only transitions, and ``approve`` is the only path to ``published`` anywhere
  in the system.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import ValidationError
from sqlalchemy import func as sa_func
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.console.schemas import (
    ArticleDetailOut,
    CardPreviewOut,
    ContactRequestOut,
    CreateRunRequest,
    DiscoveryCandidateOut,
    DismissCandidateRequest,
    GeneratedFrameOut,
    IllustrationOut,
    LoginRequest,
    MediaUploadOut,
    QueuePageOut,
    RegenerateIllustrationRequest,
    RejectRequest,
    ReviewerOut,
    RunOut,
    RunPageOut,
    SaveContentRequest,
    SetContactStatusRequest,
    SetSubjectRequest,
    StageRunOut,
    SuppressedTopicOut,
    TokenResponse,
    TrendScanOut,
)
from app.api.deps import (
    ClientIpDep,
    IllustrationThrottleDep,
    LoginThrottleDep,
    ReviewerDep,
    SessionDep,
    SettingsDep,
    require_reviewer,
)
from app.db import get_session_factory
from app.discovery.manual import ScanAlreadyRunning, manual_scan
from app.domain.contracts import Illustration
from app.domain.enums import (
    ArticleStatus,
    ContactStatus,
    DiscoveryCandidateStatus,
    DiscoveryScanStatus,
    RunOrigin,
    RunStatus,
    UserRole,
)
from app.domain.models import (
    DiscoveryCandidate,
    DiscoveryDescriptor,
    DiscoveryScan,
    PipelineRun,
    PipelineStageRun,
    User,
)
from app.imagery.base import ImageError
from app.imagery.factory import build_image_client
from app.llm.base import TokenUsage
from app.llm.pricing import cost_usd, total_cost_usd
from app.security.auth import (
    AuthenticatedReviewer,
    create_access_token,
    equalise_timing,
    verify_password,
)
from app.services.contact import ContactError, ContactService
from app.services.illustration import BOTH_FRAMES, fresh_seed, generate_illustration
from app.services.media import UnsupportedMediaError, store_image
from app.services.review import ReviewError, ReviewService

logger = logging.getLogger(__name__)

# Unauthenticated: login must be reachable without a token.
auth_router = APIRouter(prefix="/auth", tags=["console:auth"])

# Everything else.
router = APIRouter(dependencies=[Depends(require_reviewer)], tags=["console"])

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Incorrect email or password",
    headers={"WWW-Authenticate": "Bearer"},
)


# --- auth ------------------------------------------------------------------


@auth_router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    session: SessionDep,
    settings: SettingsDep,
    throttle: LoginThrottleDep,
    ip: ClientIpDep,
) -> TokenResponse:
    """Exchange email + password for a bearer token.

    Identical 401 for unknown email and wrong password, and a dummy hash
    verification on the not-found branch so both cost the same — otherwise
    response timing enumerates valid reviewer accounts.

    **The throttle check comes first, before the query and before any
    hashing.** Argon2id is memory-hard by design, so an unthrottled login
    endpoint is a CPU amplifier as much as a password oracle — and
    ``equalise_timing`` means junk input costs the same as a real attempt.
    Checking after the lookup would protect the password and not the process.

    Failures are counted for an unknown email exactly as for a wrong password.
    Counting only real accounts would make the 429 an existence oracle and
    give back the enumeration resistance the equal timing buys.
    """
    email = payload.email.strip().lower()

    retry_after = throttle.retry_after(ip=ip, email=email)
    if retry_after is not None:
        logger.warning("login refused from %s: throttled", ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again later.",
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )

    result = await session.execute(
        select(User).where(User.email == email, User.is_active.is_(True))
    )
    user = result.scalar_one_or_none()

    if user is None:
        equalise_timing()
        throttle.record_failure(ip=ip, email=email)
        raise _INVALID_CREDENTIALS

    if not verify_password(payload.password, user.password_hash):
        throttle.record_failure(ip=ip, email=email)
        raise _INVALID_CREDENTIALS

    throttle.record_success(ip=ip, email=email)
    reviewer = AuthenticatedReviewer(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=UserRole(user.role),
    )
    token, expires_in = create_access_token(reviewer, settings.jwt_secret)
    logger.info("reviewer %s logged in", user.id)
    return TokenResponse(access_token=token, expires_in=expires_in)


@auth_router.get("/me", response_model=ReviewerOut, dependencies=[Depends(require_reviewer)])
async def me(reviewer: ReviewerDep) -> ReviewerOut:
    return ReviewerOut(
        id=reviewer.id,
        email=reviewer.email,
        display_name=reviewer.display_name,
        role=reviewer.role,
    )


# --- review queue ----------------------------------------------------------


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
async def get_article_detail(article_id: str, session: SessionDep) -> ArticleDetailOut:
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
    article_id: str,
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
    article_id: str,
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
async def approve(article_id: str, session: SessionDep, reviewer: ReviewerDep) -> None:
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
    article_id: str,
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
async def get_card_preview(article_id: str, session: SessionDep) -> CardPreviewOut:
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
    article_id: str,
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


# --- media -----------------------------------------------------------------


@router.post("/media", response_model=MediaUploadOut, status_code=status.HTTP_201_CREATED)
async def upload_media(
    file: Annotated[UploadFile, File(description="A PNG, JPEG, GIF or WebP image")],
    session: SessionDep,
    settings: SettingsDep,
    reviewer: ReviewerDep,
) -> MediaUploadOut:
    """Store an image the reviewer picked on their own machine.

    Not scoped to an article, and this is also why the bytes are keyed by their
    digest in a table of their own rather than hung off ``articles``. The store
    is content-addressed and an image is referenced only by the document that
    embeds it, so an article id here would be a claim about ownership that
    nothing could keep true once the reviewer moves the image between drafts.

    The upload's filename and Content-Type are never trusted: the response
    reports the type sniffed from the bytes, and the stored filename is their
    digest. See ``services/media.py`` for why that matters when the same
    directory is served back over HTTP.

    413 when the file is over the ceiling, 415 when the bytes are not one of
    the four formats a browser can render without executing anything.
    """
    ceiling = settings.media_max_bytes
    # Read one byte past the ceiling: enough to know it was exceeded, never
    # enough for an oversized upload to be buffered whole.
    data = await file.read(ceiling + 1)
    if len(data) > ceiling:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Images must be under {ceiling // (1024 * 1024)} MB.",
        )
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="The file was empty."
        )

    try:
        # The bytes go to `media_objects`, not to disk. No commit here: the
        # request-scoped session commits on a clean return, as it does for
        # every other write on this router.
        stored = await store_image(data, session=session)
    except UnsupportedMediaError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc

    logger.info(
        "reviewer %s uploaded %s (%d bytes) as %s",
        reviewer.email,
        stored.content_type,
        stored.size,
        stored.src,
    )
    return MediaUploadOut(
        src=stored.src, content_type=stored.content_type, bytes=stored.size
    )


# --- pipeline --------------------------------------------------------------


@router.post(
    "/pipeline/runs", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED
)
async def create_run(
    payload: CreateRunRequest, session: SessionDep, reviewer: ReviewerDep
) -> RunOut:
    """Enqueue a topic for draft generation.

    Inserts a ``queued`` run and returns immediately — generation takes
    minutes, so this is 202 and the client polls the runs list. The worker
    picks it up.

    Note that no response from this endpoint can ever produce a published
    article; the run's terminal state is a draft awaiting a human.
    """
    run = await _enqueue_run(
        session,
        topic=payload.topic,
        blurb=payload.blurb,
        reviewer_id=reviewer.id,
        origin=RunOrigin.CONSOLE,
    )
    return _run_out(run, [])


async def _enqueue_run(
    session: AsyncSession,
    *,
    topic: str,
    blurb: str | None,
    reviewer_id: str,
    origin: RunOrigin,
) -> PipelineRun:
    """The one place a pipeline run is created.

    Extracted so promoting a trend candidate goes through the same insert as a
    reviewer typing a topic, rather than becoming a second path that can drift.
    It stays here, next to the route it came from, rather than moving into a
    service: ``create_run`` has no service layer today, and adding one for two
    callers would produce two shapes again with a layer in between.

    Does not commit — the router's session does, on a clean return, so a
    promotion's run and its candidate update land together or not at all.
    """
    run = PipelineRun(
        topic=topic,
        source_blurb=blurb,
        status=RunStatus.QUEUED,
        requested_by=reviewer_id,
        origin=origin,
        # Attempts are counted when a worker claims the run, so a queued one
        # has had none. Set explicitly rather than left to the server default,
        # which is not populated on the instance until it is re-read.
        attempts=0,
        created_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    logger.info("queued pipeline run %s (%s) for topic=%r", run.id, origin.value, topic)
    return run


@router.get("/pipeline/runs", response_model=RunPageOut)
async def list_runs(
    session: SessionDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RunPageOut:
    """Run history with per-stage status, newest first."""
    statement = select(PipelineRun).order_by(
        PipelineRun.created_at.desc(), PipelineRun.id.desc()
    )

    if cursor:
        anchor = await session.get(PipelineRun, cursor)
        if anchor is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed cursor"
            )
        statement = statement.where(
            tuple_(PipelineRun.created_at, PipelineRun.id) < (anchor.created_at, anchor.id)
        )

    result = await session.execute(statement.limit(limit + 1))
    runs = list(result.scalars())

    next_cursor = None
    if len(runs) > limit:
        runs = runs[:limit]
        next_cursor = runs[-1].id

    stages_by_run = await _load_stages(session, [run.id for run in runs])
    return RunPageOut(
        items=[_run_out(run, stages_by_run.get(run.id, [])) for run in runs],
        next_cursor=next_cursor,
    )


@router.get("/pipeline/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: str, session: SessionDep) -> RunOut:
    """One run: every stage, its error payload, timing and token cost.

    This is the answer to "why did last night's batch produce nothing" —
    without it, a validation failure and a crashed provider look identical from
    the outside.
    """
    run = await session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    stages_by_run = await _load_stages(session, [run_id])
    return _run_out(run, stages_by_run.get(run_id, []))


async def _load_stages(
    session: SessionDep, run_ids: list[str]
) -> dict[str, list[PipelineStageRun]]:
    """Stage rows for each run — the latest attempt only.

    A retried run holds one set of rows per attempt (they are kept, not
    overwritten, so a run that succeeded on its third try still shows what the
    first two did). Returning all of them would render the pipeline as six
    stages repeated N times, which reads as a bug. The console's question is
    "where is this run now", and the latest attempt answers it; ``attempts`` on
    the run says how many there were, and the earlier rows stay in the table
    for anyone debugging.
    """
    if not run_ids:
        return {}
    result = await session.execute(
        select(PipelineStageRun)
        .where(PipelineStageRun.run_id.in_(run_ids))
        .order_by(PipelineStageRun.attempt, PipelineStageRun.ordinal)
    )
    grouped: dict[str, list[PipelineStageRun]] = {}
    latest: dict[str, int] = {}
    for stage in result.scalars():
        # Ordered by attempt, so a higher one supersedes what we have so far.
        if stage.attempt > latest.get(stage.run_id, 0):
            latest[stage.run_id] = stage.attempt
            grouped[stage.run_id] = []
        grouped.setdefault(stage.run_id, []).append(stage)
    return grouped


def _stage_usage(stage: PipelineStageRun) -> TokenUsage:
    return TokenUsage(
        input_tokens=stage.input_tokens,
        output_tokens=stage.output_tokens,
        cache_read_tokens=stage.cache_read_tokens,
        cache_write_tokens=stage.cache_write_tokens,
    )


def _run_out(run: PipelineRun, stages: list[PipelineStageRun]) -> RunOut:
    # Cost is derived here rather than stored, so a price correction fixes
    # history instead of leaving it wrong — see app/llm/pricing.py. Summed from
    # the stage rows because the run's own totals span models and cannot be
    # priced; note the stage list is the *latest attempt only*, so a retried
    # run's cost is deliberately lower than its token totals imply.
    stage_costs = total_cost_usd(
        [(stage.model or "", _stage_usage(stage)) for stage in stages]
    )
    return RunOut(
        id=run.id,
        topic=run.topic,
        status=run.status,
        article_id=run.article_id,
        error=run.error,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cache_read_tokens=run.cache_read_tokens,
        cache_write_tokens=run.cache_write_tokens,
        estimated_cost_usd=stage_costs,
        attempts=run.attempts,
        next_attempt_at=run.next_attempt_at,
        heartbeat_at=run.heartbeat_at,
        stages=[
            StageRunOut(
                stage=stage.stage,
                ordinal=stage.ordinal,
                status=stage.status,
                error=stage.error,
                metrics=stage.metrics,
                model=stage.model,
                input_tokens=stage.input_tokens,
                output_tokens=stage.output_tokens,
                cache_read_tokens=stage.cache_read_tokens,
                cache_write_tokens=stage.cache_write_tokens,
                estimated_cost_usd=cost_usd(stage.model or "", _stage_usage(stage)),
                started_at=stage.started_at,
                finished_at=stage.finished_at,
            )
            for stage in stages
        ],
        created_at=run.created_at,
        finished_at=run.finished_at,
    )


# --- contact inbox ---------------------------------------------------------
#
# The reading half of "Let us know". The writing half is a public route; this
# side is reviewer-only because the rows carry an email address and free text
# written by anyone with the URL.


@router.get("/contact", response_model=list[ContactRequestOut])
async def contact_inbox(
    session: SessionDep,
    contact_status: Annotated[
        ContactStatus | None,
        Query(alias="status", description="Filter by handling state"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ContactRequestOut]:
    """Newest first. Deliberately unpaginated — see ``ContactService.inbox``."""
    rows = await ContactService(session).inbox(contact_status, limit)
    return [ContactRequestOut(**row) for row in rows]


@router.patch("/contact/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
async def set_contact_status(
    request_id: str,
    payload: SetContactStatusRequest,
    session: SessionDep,
    reviewer: ReviewerDep,
) -> None:
    """Mark a request answered or closed, or put it back in the queue.

    Reversible on purpose: closing a request is bookkeeping, not a decision
    anyone is answerable for, and the trail of who last touched it is kept
    either way.
    """
    try:
        await ContactService(session).set_status(request_id, payload.status, reviewer.id)
    except ContactError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# --- trend discovery -------------------------------------------------------
#
# The desk half of `scripts/scan_trends.py`. The scan proposes; a reviewer
# decides. Nothing here is reachable by the scan itself, which is the point:
# promoting a candidate spends a full generation run, and that spend is a human
# decision — the same principle as invariant #1, one step earlier in the chain.


def _candidate_out(
    candidate: DiscoveryCandidate, substance: str, outcome: str | None
) -> DiscoveryCandidateOut:
    rationale = candidate.rationale or {}
    return DiscoveryCandidateOut(
        id=candidate.id,
        topic=candidate.topic,
        substance_ui=candidate.substance_ui,
        substance_name=substance,
        outcome_name=outcome,
        score=candidate.score,
        paper_count=candidate.paper_count,
        # Falls back to the substance count for rows written before angles
        # existed, so an old proposal renders as a whole-substance one rather
        # than as an article resting on zero papers.
        angle_paper_count=int(
            rationale.get("angle_paper_count") or candidate.paper_count
        ),
        baseline_count=candidate.baseline_count,
        lift=float(rationale.get("lift", 1.0)),
        study_mix=rationale.get("study_mix", {}),
        top_pmids=rationale.get("top_pmids", []),
        status=candidate.status,
        pipeline_run_id=candidate.pipeline_run_id,
        scanned_at=candidate.created_at,
    )


@router.get("/discovery/candidates", response_model=list[DiscoveryCandidateOut])
async def list_candidates(
    session: SessionDep,
    reviewer: ReviewerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[DiscoveryCandidateOut]:
    """Live proposals, best first.

    Unpaginated with a capped limit, like the contact inbox: the scan writes at
    most `discovery_max_candidates` per run and expires what it no longer ranks,
    so this list is bounded by design rather than by the query.
    """
    substance = aliased(DiscoveryDescriptor)
    outcome = aliased(DiscoveryDescriptor)
    rows = await session.execute(
        select(DiscoveryCandidate, substance.name, outcome.name)
        .join(substance, substance.ui == DiscoveryCandidate.substance_ui)
        .outerjoin(outcome, outcome.ui == DiscoveryCandidate.outcome_ui)
        .where(DiscoveryCandidate.status == DiscoveryCandidateStatus.PROPOSED)
        .order_by(DiscoveryCandidate.score.desc())
        .limit(limit)
    )
    return [
        _candidate_out(candidate, substance_name, outcome_name)
        for candidate, substance_name, outcome_name in rows
    ]


async def _load_proposed(session: AsyncSession, candidate_id: str) -> DiscoveryCandidate:
    candidate = await session.get(DiscoveryCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "candidate not found")
    if candidate.status is not DiscoveryCandidateStatus.PROPOSED:
        # 409, not 404: it exists and the reviewer can see it — someone else
        # already acted on it, or a later scan expired it. Saying "not found"
        # would send them looking for a bug.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"candidate is {candidate.status.value}, not proposed",
        )
    return candidate


@router.post(
    "/discovery/candidates/{candidate_id}/promote",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def promote_candidate(
    candidate_id: str, session: SessionDep, reviewer: ReviewerDep
) -> RunOut:
    """Turn a proposal into a queued generation run.

    Goes through ``_enqueue_run`` — the same insert ``create_run`` uses — rather
    than writing its own row, so there is one definition of what a queued run
    is. The candidate's ``topic`` is taken verbatim rather than recomposed: what
    the reviewer read is what runs.

    Both writes are in the request's transaction, so a run without its candidate
    update (or the reverse) is not a state this can produce.
    """
    candidate = await _load_proposed(session, candidate_id)
    run = await _enqueue_run(
        session,
        topic=candidate.topic,
        blurb=None,
        reviewer_id=reviewer.id,
        origin=RunOrigin.DISCOVERY,
    )
    candidate.status = DiscoveryCandidateStatus.PROMOTED
    candidate.pipeline_run_id = run.id
    candidate.decided_by = reviewer.id
    candidate.decided_at = datetime.now(UTC)
    await session.flush()
    return _run_out(run, [])


@router.post(
    "/discovery/candidates/{candidate_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_candidate(
    candidate_id: str,
    payload: DismissCandidateRequest,
    session: SessionDep,
    reviewer: ReviewerDep,
) -> None:
    """Say no, with a reason.

    The reason is stored and shown in the next scan's dry-run output, and the
    substance is suppressed until both the cooloff elapses *and* its literature
    has doubled — see ``discovery/service.py::suppression_reason``. Without both
    gates a dismissed topic returns every fortnight with two more papers, which
    is how a proposal queue teaches people to stop reading it.
    """
    candidate = await _load_proposed(session, candidate_id)
    candidate.status = DiscoveryCandidateStatus.DISMISSED
    candidate.dismiss_reason = payload.reason
    candidate.decided_by = reviewer.id
    candidate.decided_at = datetime.now(UTC)
    await session.flush()


async def _scan_out(session: AsyncSession) -> TrendScanOut:
    """This process's last press, plus when a scan last succeeded anywhere.

    The two halves answer different questions and neither substitutes for the
    other: the runner knows whether the button is busy *now*, and only the
    ledger knows the cron slot ran on Monday.
    """
    last = await session.scalar(
        select(sa_func.max(DiscoveryScan.finished_at)).where(
            DiscoveryScan.status == DiscoveryScanStatus.SUCCEEDED
        )
    )
    state = manual_scan.state
    return TrendScanOut(
        status=state.status,
        started_at=state.started_at,
        finished_at=state.finished_at,
        records_seen=state.records_seen,
        observations_written=state.observations_written,
        candidates_proposed=state.candidates_proposed,
        notes=state.notes,
        suppressed=[
            SuppressedTopicOut(topic=item.topic, reason=item.reason)
            for item in state.suppressed
        ],
        error=state.error,
        last_scan_at=last,
    )


@router.get("/discovery/scan", response_model=TrendScanOut)
async def get_scan(session: SessionDep, reviewer: ReviewerDep) -> TrendScanOut:
    """Poll target for the desk's scan button. Cheap by design — one MAX()."""
    return await _scan_out(session)


@router.post(
    "/discovery/scan",
    response_model=TrendScanOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_scan(
    session: SessionDep, reviewer: ReviewerDep, settings: SettingsDep
) -> TrendScanOut:
    """Run the trend scan now, instead of waiting for the cron slot.

    202 with the state, not 200 with a result: the scan is minutes of PubMed
    requests and the reviewer's connection is not going to be held open for it.
    The desk polls ``GET`` until it stops saying ``running``.

    Safe to expose because a scan **proposes and nothing else** — no model
    calls, no queued run, no spend. Promoting what it finds is still a human
    decision, which is the whole shape of this feature.

    409 rather than a second task when one is already in flight: two concurrent
    scans double this process's draw on NCBI's per-IP ceiling — shared with the
    worker — to compute the same answer twice.
    """
    try:
        manual_scan.start(settings=settings, factory=get_session_factory())
    except ScanAlreadyRunning as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _scan_out(session)
