"""Authentication for the editorial console.

Deliberately minimal — one admin, JWT bearer, no invite flow — but not a shared
password. ``reviewed_by`` is a real foreign key to ``users``, so "who approved
this article" stays answerable indefinitely. That is the part that must not be
compromised for convenience; everything else here can be replaced later without
touching the audit trail.

Argon2id rather than bcrypt: no 72-byte silent truncation, and the memory-hard
parameters are current best practice.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.domain.enums import UserRole

logger = logging.getLogger(__name__)

ACCESS_TOKEN_TTL = timedelta(hours=12)
JWT_ALGORITHM = "HS256"

_hasher = PasswordHasher()

#: A real Argon2 hash of a throwaway value. Verified when a login names an
#: unknown email, so the response takes the same time as a wrong password —
#: otherwise timing enumerates valid reviewer accounts.
DUMMY_HASH = _hasher.hash("timing-equalisation-placeholder")


class AuthError(Exception):
    """Authentication failed.

    Always surfaces as 401 with a generic message. Never distinguishes expired
    from malformed from unknown-user to the client.
    """


#: Which side of the product a token speaks for.
#:
#: Readers sign themselves up; reviewers are seeded by hand. The two live in
#: different tables, so a reader id already fails ``require_reviewer`` — this
#: claim is the second lock on the same door, checked before the lookup, so a
#: token issued for one surface is rejected as malformed on the other rather
#: than merely failing to resolve. Never widen this to a role: role is a
#: property of a `users` row and is re-read on every request precisely so a
#: token cannot assert it.
TOKEN_TYPE_REVIEWER = "reviewer"
TOKEN_TYPE_READER = "reader"

#: Readers stay signed in for longer than reviewers. A review sitting is an
#: hour at a desk; a reader comes back next week and should not have to think
#: about it. Both are still bearer tokens with an expiry.
READER_TOKEN_TTL = timedelta(days=30)

#: A password-reset link. Short, because it travels by email.
TOKEN_TYPE_READER_RESET = "reader_reset"
RESET_TOKEN_TTL = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class AuthenticatedReviewer:
    """Resolved from a bearer token. Passed into every review mutation."""

    id: str
    email: str
    display_name: str
    role: UserRole


@dataclass(frozen=True, slots=True)
class AuthenticatedReader:
    """A signed-in member of the public. Carries no role, and never will.

    Deliberately a different type from :class:`AuthenticatedReviewer` rather
    than the same one with an empty role: a function that takes a reviewer
    cannot be handed a reader by accident, and the type checker says so at the
    call site instead of the database saying so at the query.
    """

    id: str
    email: str
    display_name: str


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Constant-time verification.

    Returns False rather than raising on a mismatch, so callers cannot
    accidentally distinguish "wrong password" from "malformed hash" in a
    response.
    """
    try:
        return _hasher.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def check_password(plaintext: str, hashed: str) -> bool:
    """``verify_password`` in a thread, for request handlers.

    Argon2 costs ~33 ms; on the event loop that stalls every other request,
    the public feed included, for the length of each login attempt.
    """
    return await asyncio.to_thread(verify_password, plaintext, hashed)


async def equalise_timing() -> None:
    """Burn one hash verification for an unknown email.

    Call this on the not-found branch of login so both branches cost the same.
    """
    await check_password("wrong", DUMMY_HASH)


def create_access_token(reviewer: AuthenticatedReviewer, secret: str) -> tuple[str, int]:
    """Issue a JWT. Returns (token, expires_in_seconds).

    Claims are ``sub``, ``role``, ``exp``, ``iat`` — deliberately no email or
    display name. Those change, and a token asserting a stale one is worse than
    a token that requires a lookup.
    """
    now = datetime.now(UTC)
    expires = now + ACCESS_TOKEN_TTL
    payload = {
        "sub": reviewer.id,
        "role": str(reviewer.role),
        "typ": TOKEN_TYPE_REVIEWER,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)
    return token, int(ACCESS_TOKEN_TTL.total_seconds())


def create_reader_token(reader_id: str, secret: str, password_hash: str) -> tuple[str, int]:
    """Issue a reader's JWT. Returns (token, expires_in_seconds).

    Same signing key and algorithm as the reviewer token, separated by the
    ``typ`` claim rather than by a second secret. One secret to rotate, and the
    separation is checked on every decode — see ``decode_token_subject``.

    It also carries a fingerprint of the password hash, so a password reset
    signs out every device: see ``_resolve_reader`` in ``api/deps.py``.
    """
    now = datetime.now(UTC)
    expires = now + READER_TOKEN_TTL
    payload = {
        "sub": reader_id,
        "typ": TOKEN_TYPE_READER,
        "pwd": _password_fingerprint(password_hash),
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)
    return token, int(READER_TOKEN_TTL.total_seconds())


def _password_fingerprint(password_hash: str) -> str:
    """Changes whenever the password does, which is what makes a reset link single-use."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def create_reset_token(reader_id: str, password_hash: str, secret: str) -> str:
    """A signed, 30-minute reset link token, no table needed.

    It carries a fingerprint of the current password hash, so it stops working
    the moment the password changes, its own use included. Its ``typ`` means
    neither surface accepts it as a session token.
    """
    now = datetime.now(UTC)
    payload = {
        "sub": reader_id,
        "typ": TOKEN_TYPE_READER_RESET,
        "pwd": _password_fingerprint(password_hash),
        "iat": int(now.timestamp()),
        "exp": int((now + RESET_TOKEN_TTL).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def reset_token_subject(token: str, secret: str) -> tuple[str, str]:
    """(reader id, password fingerprint) from a reset token, or ``AuthError``."""
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise AuthError("invalid token") from exc
    subject, fingerprint = payload.get("sub"), payload.get("pwd")
    if (
        payload.get("typ") != TOKEN_TYPE_READER_RESET
        or not isinstance(subject, str)
        or not isinstance(fingerprint, str)
    ):
        raise AuthError("invalid token")
    return subject, fingerprint


def password_unchanged(fingerprint: str, password_hash: str) -> bool:
    """True while the password is the one a token was issued against."""
    return hmac.compare_digest(fingerprint, _password_fingerprint(password_hash))


def decode_reader_token(token: str, secret: str) -> tuple[str, str | None]:
    """(reader id, password fingerprint) from a reader session token.

    shortcut: tokens issued before 2026-11-10 carry no fingerprint and give None;
    they all expire by 2026-12-10, after which None can be refused.
    """
    payload = _decode(token, secret, TOKEN_TYPE_READER)
    fingerprint = payload.get("pwd")
    return payload["sub"], fingerprint if isinstance(fingerprint, str) else None


def decode_token_subject(
    token: str, secret: str, *, expect: str = TOKEN_TYPE_REVIEWER
) -> str:
    """Verify the signature and return the subject id.

    Deliberately does **not** trust the token's claims beyond identity and
    audience. The caller re-loads the account to confirm it still exists and is
    active — a signature-valid token for a deactivated reviewer must not
    authenticate, and a claims-only check would allow exactly that for the
    token's full life.

    ``expect`` is the one claim that *is* trusted, because it is not an
    assertion about the account: it records which endpoint minted the token.
    A reader token presented to the console fails here rather than a few lines
    later at the ``users`` lookup, so the two surfaces cannot be conflated by a
    future change to either query.

    Raises:
        AuthError: expired, malformed, bad signature, or minted for the other
            surface.
    """
    return str(_decode(token, secret, expect)["sub"])


def _decode(token: str, secret: str, expect: str) -> dict[str, Any]:
    """Signature, expiry, surface and a non-empty subject, or ``AuthError``."""
    try:
        payload: dict[str, Any] = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise AuthError("invalid token") from exc

    if payload.get("typ", TOKEN_TYPE_REVIEWER) != expect:
        raise AuthError("invalid token")

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AuthError("invalid token")
    return payload
