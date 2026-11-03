"""Email the editorial inbox when a "Let us know" message arrives.

Two rules:

* **A failed email never fails the submission.** It runs as a background task
  after the row is committed, and every error is logged and swallowed. The
  ``contact_requests`` row is the record; the email is a nudge to go read it.
* **The sender's words go only in the body, as plain text.** The subject is
  built from the kind and the name with every line break collapsed, so a name
  cannot add headers (``Bcc:``) to the message. ``Reply-To`` is the sender's
  validated address, so pressing Reply answers them.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app.config import Settings
from app.domain.enums import ContactKind
from app.domain.models import ContactRequest

logger = logging.getLogger(__name__)

_KIND_LABELS = {
    ContactKind.TOPIC: "Suggest a topic",
    ContactKind.OTHER: "Something else",
    ContactKind.FACT_CHECK: "Fact-check a claim",
}


def contact_email(request: ContactRequest, settings: Settings) -> EmailMessage:
    """The message for one submission. Built while the row is still loaded."""
    kind = _KIND_LABELS.get(request.kind, str(request.kind))
    name = " ".join((request.name or "").split())
    message = EmailMessage()
    message["Subject"] = f"[Let us know] {kind} from {name}"
    message["From"] = settings.smtp_user
    message["To"] = settings.contact_notify_to
    message["Reply-To"] = request.email
    message.set_content(
        "\n".join(
            [
                f"Kind:  {kind}",
                f"Name:  {name}",
                f"Email: {request.email}",
                f"Link:  {request.link or '-'}",
                f"Ref:   {request.id}",
                "",
                request.note or "(no description, only the link)",
                "",
                "Reply to this email to answer the sender.",
            ]
        )
    )
    return message


def _send(message: EmailMessage, settings: Settings) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


async def notify_editors(message: EmailMessage, settings: Settings) -> None:
    """Send it, off the event loop. Unconfigured or failing: log, never raise."""
    if not settings.smtp_password:
        return
    try:
        await asyncio.to_thread(_send, message, settings)
    except Exception:
        logger.exception("contact notification to the editorial inbox was not sent")
