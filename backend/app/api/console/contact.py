"""The contact inbox: what readers sent through Let us know."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.console.schemas import (
    ContactRequestOut,
    SetContactStatusRequest,
)
from app.api.deps import (
    IdPath,
    ReviewerDep,
    SessionDep,
    require_reviewer,
)
from app.domain.enums import (
    ContactStatus,
)
from app.services.contact import ContactError, ContactService

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_reviewer)], tags=["console"])


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
    request_id: IdPath,
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
