"""What "Let us know" refuses: no name, or neither a link nor a description."""

from typing import Any

import pytest

from app.domain.enums import ContactKind
from app.services.contact import ContactError, ContactService


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


async def _submit(**fields: Any) -> _Session:
    session = _Session()
    base = {"kind": ContactKind.TOPIC, "email": "a@example.com", "name": "Ada", "note": ""}
    await ContactService(session).submit(**(base | fields))  # type: ignore[arg-type]
    return session


async def test_a_link_alone_is_enough() -> None:
    session = await _submit(link="https://example.com/video")
    assert session.added[0].note == ""
    assert session.added[0].link == "https://example.com/video"


async def test_a_description_alone_is_enough() -> None:
    session = await _submit(note="  creatine and sleep  ")
    assert session.added[0].note == "creatine and sleep"


@pytest.mark.parametrize(
    "fields",
    [{"note": "  ", "link": " "}, {"note": "hi", "name": "  "}, {"note": "hi", "name": None}],
)
async def test_missing_name_or_content_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ContactError):
        await _submit(**fields)
