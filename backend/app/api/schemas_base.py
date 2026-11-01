"""Shared base for every API response model.

The wire format is **camelCase**; Python stays snake_case. Without this the
TypeScript client would have to either mirror Python naming or remap every
field by hand, and hand-remapping is where silent contract drift starts.

``populate_by_name=True`` means handlers can still construct these with Python
field names — ``ArticleOut(**service_dict)`` works with the snake_case dicts
the service layer returns, and only serialisation is camelCased.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints
from pydantic.alias_generators import to_camel

#: An email address on the wire.
#:
#: A shape check, not ``pydantic.EmailStr``. The full validator pulls in
#: ``email-validator`` and its DNS-adjacent rules to reject addresses that are
#: technically legal, which buys nothing here: the only thing this system does
#: with an address is send to it, and delivery is the real test either way.
#: What the constraint does buy is a normalised value — trimmed and lowercased
#: before it reaches the unique index — so `Ada@Example.com` and
#: `ada@example.com ` cannot become two accounts.
#:
#: 254 is the RFC 5321 ceiling on a full address.
EmailAddress = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        max_length=254,
        pattern=r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$",
    ),
]


#: A row id (``gen_random_uuid()``) arriving from a client. Checked at the
#: boundary because an id that is not a UUID otherwise reaches Postgres, which
#: refuses it, and the API answered 500 instead of a client error.
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
UuidStr = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
        from_attributes=True,
    )
