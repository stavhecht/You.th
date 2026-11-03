"""The editorial email for a "Let us know" message (``services/notify.py``)."""

from typing import Any

import pytest

from app.config import Settings
from app.domain.enums import ContactKind
from app.domain.models import ContactRequest
from app.services import notify

_SETTINGS = Settings(
    smtp_user="sender@gmail.com",
    smtp_password="app-password",
    contact_notify_to="desk@gmail.com",
)


def _request(**fields: Any) -> ContactRequest:
    base: dict[str, Any] = {
        "id": "3f1c0000-0000-0000-0000-000000000000",
        "kind": ContactKind.TOPIC,
        "name": "Maya",
        "email": "maya@example.com",
        "link": "https://example.com/video",
        "note": "Creatine for sleep?",
    }
    return ContactRequest(**(base | fields))


def test_the_email_carries_every_field_and_replies_to_the_sender() -> None:
    message = notify.contact_email(_request(), _SETTINGS)

    assert message["To"] == "desk@gmail.com"
    assert message["Reply-To"] == "maya@example.com"
    assert message["Subject"] == "[Let us know] Suggest a topic from Maya"
    body = message.get_content()
    for value in ("maya@example.com", "https://example.com/video", "Creatine for sleep?"):
        assert value in body
    assert "3f1c0000" in body


def test_a_name_cannot_add_headers() -> None:
    injected = _request(name="Maya\r\nBcc: everyone@example.com")
    message = notify.contact_email(injected, _SETTINGS)

    assert message["Bcc"] is None
    assert "\n" not in message["Subject"]


async def test_nothing_is_sent_without_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []
    monkeypatch.setattr(notify, "_send", lambda *args: sent.append(args))
    unconfigured = Settings(smtp_password="")

    await notify.notify_editors(notify.contact_email(_request(), unconfigured), unconfigured)

    assert sent == []


async def test_a_mail_outage_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(*_: Any) -> None:
        raise OSError("smtp.gmail.com unreachable")

    monkeypatch.setattr(notify, "_send", refuse)

    await notify.notify_editors(notify.contact_email(_request(), _SETTINGS), _SETTINGS)

    assert "was not sent" in caplog.text


def test_sending_a_message_queues_one_email_after_saving_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route's wiring: without it every test above passes and no email goes out."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    import app.api.deps as deps
    import app.api.public.contact as contact_api
    from app.config import get_settings
    from app.main import create_app

    saved: list[ContactRequest] = []
    queued: list[Any] = []

    class _Session:
        def add(self, row: ContactRequest) -> None:
            row.id = "3f1c0000-0000-0000-0000-000000000000"
            saved.append(row)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def _session() -> Any:
        yield _Session()

    async def _record(message: Any, settings: Settings) -> None:
        queued.append(message)

    monkeypatch.setattr(contact_api, "notify_editors", _record)
    app = create_app()
    app.dependency_overrides[deps.get_session] = _session
    app.dependency_overrides[deps.get_contact_throttle] = lambda: SimpleNamespace(
        retry_after=lambda **_: None, record_failure=lambda **_: None
    )
    app.dependency_overrides[get_settings] = lambda: _SETTINGS

    with TestClient(app) as http:
        response = http.post(
            "/api/contact",
            json={"kind": "topic", "name": "Maya", "email": "maya@example.com", "note": "Hi"},
        )

    assert response.status_code == 202
    assert len(saved) == 1
    recipients = [(m["To"], m["Reply-To"]) for m in queued]
    assert recipients == [("desk@gmail.com", "maya@example.com")]
