"""The public half of "Let us know". One route, write-only.

There is no GET here on purpose. Reading the inbox is a console route
(``/api/console/contact``) because the rows carry an email address and free
text written by anyone with the URL.

Rate-limited by IP through the same token-bucket the reader login uses. Without
it this is an open write endpoint on a table with a text column, which is a
spam sink rather than an inbox. The limit rejects rather than sleeps — same
reasoning as ``security/login_throttle.py``: a delay only slows a client that
chooses to wait.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, status

from app.api.deps import (
    ClientIpDep,
    ContactThrottleDep,
    OptionalReaderDep,
    SessionDep,
    SettingsDep,
    too_many_attempts,
)
from app.api.public.schemas import ContactRequestIn
from app.domain.enums import ContactKind
from app.services.contact import ContactError, ContactService
from app.services.notify import contact_email, notify_editors

logger = logging.getLogger(__name__)

router = APIRouter(tags=["contact"])


@router.post("/contact", status_code=status.HTTP_202_ACCEPTED)
async def submit_contact(
    payload: ContactRequestIn,
    session: SessionDep,
    throttle: ContactThrottleDep,
    ip: ClientIpDep,
    settings: SettingsDep,
    background: BackgroundTasks,
    reader: OptionalReaderDep,
) -> dict[str, str]:
    """Record a request for the editorial team.

    202 rather than 201: the row exists, but the thing the sender is asking for
    — a person reading it, and possibly an article — has not happened and may
    not. A 201 would promise a resource that does not follow.

    The bucket is keyed on the submitted address as well as the IP, matching
    the login throttle's signature, so one address cannot walk around the IP
    limit through a proxy pool and one IP cannot walk around it by varying the
    address.
    """
    # Suggesting a topic is for signed-in readers, and it is sent as them: the
    # name and address come from the account, not from the form.
    name, email = payload.name, payload.email
    if payload.kind == ContactKind.TOPIC:
        if reader is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sign in to suggest a topic",
            )
        name, email = reader.display_name, reader.email

    retry_after = throttle.retry_after(ip=ip, email=email)
    if retry_after is not None:
        raise too_many_attempts(retry_after, "Too many messages from here. Try again later.")
    # Counted here, with no `await` between check and count: counted after the
    # save, 50 parallel requests all passed the check before the first was
    # counted, and each one was saved and emailed. Every attempt spends the
    # budget, refused ones too; a person sending one message a week never
    # reaches the limit.
    throttle.record_failure(ip=ip, email=email)

    try:
        request = await ContactService(session).submit(
            kind=payload.kind,
            email=email,
            note=payload.note,
            name=name,
            link=payload.link,
        )
    except ContactError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from None

    # Runs after the response, and the session commits before it (SessionDep's
    # scope="function"), so an email only ever describes a saved row.
    background.add_task(notify_editors, contact_email(request, settings), settings)
    return {"id": request.id}
