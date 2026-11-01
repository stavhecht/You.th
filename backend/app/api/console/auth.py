"""Console login and the signed-in reviewer."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.console.schemas import (
    LoginRequest,
    ReviewerOut,
    TokenResponse,
)
from app.api.deps import (
    ClientIpDep,
    LoginThrottleDep,
    ReviewerDep,
    SessionDep,
    SettingsDep,
    require_reviewer,
    too_many_attempts,
)
from app.domain.enums import (
    UserRole,
)
from app.domain.models import (
    User,
)
from app.security.auth import (
    AuthenticatedReviewer,
    check_password,
    create_access_token,
    equalise_timing,
)

logger = logging.getLogger(__name__)

# Unauthenticated: login must be reachable without a token.
auth_router = APIRouter(prefix="/auth", tags=["console:auth"])

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Incorrect email or password",
    headers={"WWW-Authenticate": "Bearer"},
)


@auth_router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    session: SessionDep,
    settings: SettingsDep,
    throttle: LoginThrottleDep,
    ip: ClientIpDep,
) -> TokenResponse:
    """Exchange email + password for a bearer token.

    Identical 401 for unknown email and wrong password, and a dummy hash
    verification on the not-found branch so both cost the same — otherwise
    response timing enumerates valid reviewer accounts.

    **The throttle check comes first, before the query and before any
    hashing.** Argon2id is memory-hard by design, so an unthrottled login
    endpoint is a CPU amplifier as much as a password oracle — and
    ``equalise_timing`` means junk input costs the same as a real attempt.
    Checking after the lookup would protect the password and not the process.

    Failures are counted for an unknown email exactly as for a wrong password.
    Counting only real accounts would make the 429 an existence oracle and
    give back the enumeration resistance the equal timing buys.
    """
    email = payload.email.strip().lower()

    retry_after = throttle.retry_after(ip=ip, email=email)
    if retry_after is not None:
        logger.warning("login refused from %s: throttled", ip)
        raise too_many_attempts(retry_after, "Too many login attempts. Try again later.")

    result = await session.execute(
        select(User).where(User.email == email, User.is_active.is_(True))
    )
    user = result.scalar_one_or_none()

    if user is None:
        await equalise_timing()
        throttle.record_failure(ip=ip, email=email)
        raise _INVALID_CREDENTIALS

    if not await check_password(payload.password, user.password_hash):
        throttle.record_failure(ip=ip, email=email)
        raise _INVALID_CREDENTIALS

    throttle.record_success(ip=ip, email=email)
    reviewer = AuthenticatedReviewer(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=UserRole(user.role),
    )
    token, expires_in = create_access_token(reviewer, settings.jwt_secret)
    logger.info("reviewer %s logged in", user.id)
    return TokenResponse(access_token=token, expires_in=expires_in)


@auth_router.get("/me", response_model=ReviewerOut, dependencies=[Depends(require_reviewer)])
async def me(reviewer: ReviewerDep) -> ReviewerOut:
    return ReviewerOut(
        id=reviewer.id,
        email=reviewer.email,
        display_name=reviewer.display_name,
        role=reviewer.role,
    )
