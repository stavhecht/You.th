"""Image uploads from the reviewer's machine."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.console.schemas import (
    MediaUploadOut,
)
from app.api.deps import (
    ReviewerDep,
    SessionDep,
    SettingsDep,
    require_reviewer,
)
from app.services.media import UnsupportedMediaError, store_image

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_reviewer)], tags=["console"])


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
    # Read one byte past the ceiling: enough to know it was exceeded without
    # holding an oversized upload in memory. It does not stop one arriving:
    # Starlette has already spooled the whole file to a temp file by now (its
    # part limit covers form fields, not files). Acceptable while only signed-in
    # reviewers reach this route; a public upload would need a body-size limit
    # in front of the app.
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
