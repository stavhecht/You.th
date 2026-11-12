"""Console login, the signed-in reviewer, and their own account."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select

from app.api.console.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    ReviewerForgotRequest,
    ReviewerOut,
    ReviewerResetRequest,
    TokenResponse,
)
from app.api.deps import (
    ClientIpDep,
    LoginThrottleDep,
    ReviewerDep,
    ReviewerResetThrottleDep,
    SessionDep,
    SettingsDep,
    require_reviewer,
    too_many_attempts,
)
from app.domain.enums import UserRole
from app.domain.models import User
from app.security.auth import (
    TOKEN_TYPE_REVIEWER_RESET,
    AuthenticatedReviewer,
    AuthError,
    check_password,
    create_access_token,
    create_reset_token,
    equalise_timing,
    hash_password,
    password_unchanged,
    reset_token_subject,
)
from app.services.notify import password_reset_email, send_email

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
    logger.info("reviewer %s logged in", user.id)
    return _token_for(user, settings.jwt_secret)


def _token_for(user: User, secret: str) -> TokenResponse:
    reviewer = AuthenticatedReviewer(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=UserRole(user.role),
    )
    token, expires_in = create_access_token(reviewer, secret, user.password_hash)
    return TokenResponse(access_token=token, expires_in=expires_in)


@auth_router.get("/me", response_model=ReviewerOut, dependencies=[Depends(require_reviewer)])
async def me(reviewer: ReviewerDep) -> ReviewerOut:
    return ReviewerOut(
        id=reviewer.id,
        email=reviewer.email,
        display_name=reviewer.display_name,
        role=reviewer.role,
    )


async def _confirm_password(
    password: str,
    reviewer: AuthenticatedReviewer,
    session: SessionDep,
    throttle: LoginThrottleDep,
    ip: str,
) -> User:
    """Re-check the reviewer's password before changing how they sign in.

    Through the login throttle and before any hashing, for the reason
    ``DELETE /readers/me`` does it: without it a stolen token turns these
    routes into an unthrottled password oracle for its own account.
    """
    retry_after = throttle.retry_after(ip=ip, email=reviewer.email)
    if retry_after is not None:
        raise too_many_attempts(retry_after, "Too many attempts. Try again later.")
    user = await session.get(User, reviewer.id)
    if user is None or not await check_password(password, user.password_hash):
        throttle.record_failure(ip=ip, email=reviewer.email)
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="That password is not right.")
    return user


@auth_router.post("/password", response_model=TokenResponse)
async def change_password(
    payload: ChangePasswordRequest,
    reviewer: ReviewerDep,
    session: SessionDep,
    settings: SettingsDep,
    throttle: LoginThrottleDep,
    ip: ClientIpDep,
) -> TokenResponse:
    """Change the signed-in reviewer's password.

    Every other session stops working at once (the token fingerprint), so the
    answer carries a fresh token for this one.
    """
    user = await _confirm_password(payload.current_password, reviewer, session, throttle, ip)
    user.password_hash = await asyncio.to_thread(hash_password, payload.new_password)
    await session.flush()
    logger.info("reviewer %s changed their password", user.id)
    return _token_for(user, settings.jwt_secret)


_EXPIRED_LINK = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail="This reset link has expired or was already used. Ask for a new one.",
)


@auth_router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    payload: ReviewerForgotRequest,
    session: SessionDep,
    settings: SettingsDep,
    throttle: ReviewerResetThrottleDep,
    ip: ClientIpDep,
    background: BackgroundTasks,
) -> dict[str, str]:
    """Email a reset link to a reviewer who forgot their password.

    Unauthenticated, like login: a reviewer who forgot their password has no
    session. The answer is the same whether or not the address is a reviewer's,
    and the email goes out after the response, so neither the body nor the
    timing says which addresses are on the desk. Counted on every request,
    because every request can send mail.
    """
    email = payload.email.strip().lower()
    retry_after = throttle.retry_after(ip=ip, email=email)
    if retry_after is not None:
        raise too_many_attempts(retry_after, "Too many attempts. Try again later.")
    throttle.record_failure(ip=ip, email=email)

    user = (
        await session.execute(select(User).where(User.email == email, User.is_active.is_(True)))
    ).scalar_one_or_none()
    if user is not None:
        token = create_reset_token(
            user.id, user.password_hash, settings.jwt_secret, typ=TOKEN_TYPE_REVIEWER_RESET
        )
        link = f"{settings.public_site_url.rstrip('/')}/review/reset?token={token}"
        background.add_task(
            send_email,
            password_reset_email(
                user.email, link, settings, subject="Reset your You.th review desk password"
            ),
            settings,
            "reviewer password reset email",
        )
    return {"status": "sent if the account exists"}


@auth_router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    payload: ReviewerResetRequest, session: SessionDep, settings: SettingsDep
) -> None:
    """Set a new password from the emailed link. The link then stops working,
    and so does every session signed in with the old password.

    Not throttled, like the readers' reset: a bad token costs one signature
    check, and the slow hash runs only after a valid link.
    """
    try:
        user_id, fingerprint = reset_token_subject(
            payload.token, settings.jwt_secret, typ=TOKEN_TYPE_REVIEWER_RESET
        )
    except AuthError:
        raise _EXPIRED_LINK from None
    user = await session.get(User, user_id)
    if user is None or not user.is_active or not password_unchanged(
        fingerprint, user.password_hash
    ):
        raise _EXPIRED_LINK
    user.password_hash = await asyncio.to_thread(hash_password, payload.password)
    await session.flush()
    logger.info("reviewer %s reset their password", user.id)
