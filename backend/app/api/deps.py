"""FastAPI dependencies: database sessions and the auth gate."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas_base import UUID_PATTERN
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.domain.enums import UserRole
from app.domain.models import Reader, User
from app.security.auth import (
    TOKEN_TYPE_READER,
    AuthenticatedReader,
    AuthenticatedReviewer,
    AuthError,
    decode_token_subject,
)
from app.security.login_throttle import InMemoryLoginThrottle, LoginThrottle
from app.security.spend_throttle import InMemorySpendThrottle, SpendThrottle

bearer_scheme = HTTPBearer(auto_error=False)

#: One generic message for every authentication failure. Distinguishing
#: "expired" from "malformed" from "unknown user" tells an attacker which half
#: of their guess was right.
_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """Request-scoped session, committed on success and rolled back on error."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# scope="function": commit before the response is sent. FastAPI's default runs
# the exit code afterwards, so a failed commit still answered 2xx.
#: A row id in the URL. Malformed ones answer 422 before any handler runs.
IdPath = Annotated[str, Path(pattern=UUID_PATTERN)]

SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
SettingsDep = Annotated[Settings, Depends(get_settings)]

#: Process-wide, because the counters *are* the state — a per-request instance
#: would forget every failure the moment it answered. Injected as a dependency
#: rather than imported at the call site so a test can override it and a Redis
#: implementation can replace it without touching the route.
_login_throttle: LoginThrottle = InMemoryLoginThrottle()


def get_login_throttle() -> LoginThrottle:
    return _login_throttle


LoginThrottleDep = Annotated[LoginThrottle, Depends(get_login_throttle)]

#: A **separate** counter for the reader login, not the console's.
#:
#: One shared instance would make the two endpoints each other's denial of
#: service: readers and reviewers arrive from the same office NAT, and enough
#: failed reader logins would lock the console out of its own IP budget. The
#: budgets protect the same process, but they are not the same budget.
_reader_login_throttle: LoginThrottle = InMemoryLoginThrottle()


def get_reader_login_throttle() -> LoginThrottle:
    return _reader_login_throttle


ReaderLoginThrottleDep = Annotated[LoginThrottle, Depends(get_reader_login_throttle)]

#: And a third for the contact form, for the same reason again.
#:
#: It is not a login — nothing is being guessed — but it is the other
#: unauthenticated write on the public surface, and the same token bucket is
#: the right shape: an escalating, expiring, reject-don't-sleep budget per IP
#: and per address. Sharing the reader-login instance would let a burst of
#: messages lock someone out of their own account.
_contact_throttle: LoginThrottle = InMemoryLoginThrottle()


def get_contact_throttle() -> LoginThrottle:
    return _contact_throttle


ContactThrottleDep = Annotated[LoginThrottle, Depends(get_contact_throttle)]

#: And a fourth for signup, kept apart for the same reason: it hashes a password
#: on every call, so unthrottled it is the CPU amplifier the login throttle
#: exists to close, and sharing the reader-login budget would let a burst of
#: signups lock readers out of their own accounts.
_signup_throttle: LoginThrottle = InMemoryLoginThrottle()


def get_signup_throttle() -> LoginThrottle:
    return _signup_throttle


SignupThrottleDep = Annotated[LoginThrottle, Depends(get_signup_throttle)]

#: And a fourth counter, of a different kind, for regenerating article imagery.
#:
#: Not a ``LoginThrottle`` instance, because it is not counting the same thing.
#: The three above count *failures* on unauthenticated endpoints and are keyed
#: by IP and address; this one counts *successes* on an authenticated one and
#: is keyed by reviewer id. What it protects is the project's inference credit
#: rather than the process — regenerate is the only console action that bills
#: an external provider per press. See ``security/spend_throttle.py``.
_illustration_throttle: SpendThrottle = InMemorySpendThrottle()


def get_illustration_throttle() -> SpendThrottle:
    return _illustration_throttle


IllustrationThrottleDep = Annotated[SpendThrottle, Depends(get_illustration_throttle)]


def too_many_attempts(retry_after: float, detail: str) -> HTTPException:
    """The 429 a throttled login, signup or contact route answers with."""
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(max(1, int(retry_after)))},
    )


def client_ip(request: Request) -> str:
    """The socket peer, and deliberately nothing else.

    ``X-Forwarded-For`` is attacker-controlled until a trusted proxy overwrites
    it. Reading it here would let anyone mint a fresh identity per request and
    bypass the IP budget entirely — worse than no limit, because the endpoint
    would look protected. Nothing sits in front of uvicorn today, so the socket
    peer is the truth.

    Behind a load balancer this must become the forwarded address, via
    ``uvicorn --proxy-headers --forwarded-allow-ips=<balancer>`` so Starlette
    trusts the header only from the balancer (DESIGN.md §10). Until then, one
    bucket for anything with no peer address, which no real request has.
    """
    return request.client.host if request.client else "unknown"


ClientIpDep = Annotated[str, Depends(client_ip)]


async def require_reviewer(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> AuthenticatedReviewer:
    """Auth gate for every console route.

    Applied at the router level in each ``api/console/`` module, not per handler —
    a router-level dependency cannot be forgotten when someone adds a route.

    Re-loads the user rather than trusting the token's claims, so a deactivated
    reviewer stops authenticating immediately rather than at token expiry.
    """
    if credentials is None or not credentials.credentials:
        raise _UNAUTHORIZED

    try:
        user_id = decode_token_subject(credentials.credentials, settings.jwt_secret)
    except AuthError:
        raise _UNAUTHORIZED from None

    result = await session.execute(
        select(User).where(User.id == user_id, User.is_active.is_(True))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _UNAUTHORIZED

    return AuthenticatedReviewer(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=UserRole(user.role),
    )


ReviewerDep = Annotated[AuthenticatedReviewer, Depends(require_reviewer)]


async def _resolve_reader(
    session: AsyncSession,
    settings: Settings,
    credentials: HTTPAuthorizationCredentials | None,
) -> AuthenticatedReader | None:
    """Shared body of the two reader gates. Returns None for any failure."""
    if credentials is None or not credentials.credentials:
        return None

    try:
        reader_id = decode_token_subject(
            credentials.credentials, settings.jwt_secret, expect=TOKEN_TYPE_READER
        )
    except AuthError:
        return None

    result = await session.execute(
        select(Reader).where(Reader.id == reader_id, Reader.is_active.is_(True))
    )
    reader = result.scalar_one_or_none()
    if reader is None:
        return None

    return AuthenticatedReader(
        id=reader.id, email=reader.email, display_name=reader.display_name
    )


async def require_reader(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> AuthenticatedReader:
    """Auth gate for a reader's own data — their profile, folders and saves.

    Re-loads the reader for the same reason ``require_reviewer`` does: a
    deactivated account must stop authenticating immediately rather than at
    token expiry, and a reader token lasts thirty days.
    """
    reader = await _resolve_reader(session, settings, credentials)
    if reader is None:
        raise _UNAUTHORIZED
    return reader


async def optional_reader(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> AuthenticatedReader | None:
    """The feed's gate: signed in personalises the order, signed out still works.

    **Never raises.** A stale or malformed token on the public feed has to
    degrade to the anonymous feed rather than to a 401 — the alternative is a
    reader whose month-old token has expired seeing an error page where the
    site used to be, on a surface that requires no account at all.
    """
    return await _resolve_reader(session, settings, credentials)


ReaderDep = Annotated[AuthenticatedReader, Depends(require_reader)]
OptionalReaderDep = Annotated[AuthenticatedReader | None, Depends(optional_reader)]
