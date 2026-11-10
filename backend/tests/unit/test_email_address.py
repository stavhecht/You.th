"""The shape check every public email field goes through (``schemas_base.EmailAddress``)."""

import pytest
from pydantic import TypeAdapter, ValidationError

from app.api.schemas_base import EmailAddress

_email = TypeAdapter(EmailAddress)


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("ada@example.com", "ada@example.com"),
        ("  Ada.Lovelace+news@Mail.Example.CO.UK ", "ada.lovelace+news@mail.example.co.uk"),
        ("o'brien@my-site.ie", "o'brien@my-site.ie"),
        ("ada@xn--80ak6aa92e.com", "ada@xn--80ak6aa92e.com"),
        ("a@b.xn--p1ai", "a@b.xn--p1ai"),
    ],
)
def test_real_addresses_pass_and_are_normalised(raw: str, stored: str) -> None:
    assert _email.validate_python(raw) == stored


@pytest.mark.parametrize(
    "raw",
    [
        "ada",
        "ada@gmail",
        "ada@@example.com",
        "@example.com",
        "ada@",
        ".ada@example.com",
        "ada.@example.com",
        "ad..a@example.com",
        "ada@example..com",
        "ada@-example.com",
        "ada@example-.com",
        "ada@example.c",
        "ada@example.c0m",
        "ada lovelace@example.com",
        "ada@exa mple.com",
        "ada,b@example.com",
        "ada@example.com.",
        "हिंदी@example.com",
        "josé@example.com",
        "ada@exämple.com",
        "\u017fam@example.com",
        "\u212aate@example.com",
    ],
)
def test_typos_are_refused(raw: str) -> None:
    with pytest.raises(ValidationError):
        _email.validate_python(raw)
