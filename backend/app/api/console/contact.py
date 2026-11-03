"""The contact inbox: what readers sent through Let us know."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.console.schemas import ContactRequestOut
from app.api.deps import SessionDep, require_reviewer
from app.services.contact import ContactService

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_reviewer)], tags=["console"])


#
# The reading half of "Let us know". The writing half is a public route; this
# side is reviewer-only because the rows carry an email address and free text
# written by anyone with the URL.


@router.get("/contact", response_model=list[ContactRequestOut])
async def contact_inbox(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ContactRequestOut]:
    """Newest first. Deliberately unpaginated — see ``ContactService.inbox``."""
    rows = await ContactService(session).inbox(limit)
    return [ContactRequestOut(**row) for row in rows]
