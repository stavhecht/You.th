"""Reader accounts. Public, self-service, and strictly about the reader's own data.

Mounted under ``/api/readers``. Every route here either creates a session or
operates on the caller's own row — there is no listing endpoint, no lookup by
id, and nothing that can see another reader.

The login here is a copy of the console's in structure and deliberately so: the
throttle is checked **before** the lookup and before any hashing, failures are
counted for unknown addresses exactly as for wrong passwords, and both branches
cost one Argon2 verification. Those three properties are what stop the endpoint
being a CPU amplifier and an account-existence oracle, and each of them is
invisible in a behavioural test — see the note in
``app/security/login_throttle.py``.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.deps import (
    ClientIpDep,
    IdPath,
    ReaderDep,
    ReaderLoginThrottleDep,
    SessionDep,
    SettingsDep,
    SignupThrottleDep,
    too_many_attempts,
)
from app.api.public.schemas import (
    CreateFolderRequest,
    DeleteAccountRequest,
    FolderOut,
    ReaderLoginRequest,
    ReaderOut,
    ReaderTokenResponse,
    SavedPageOut,
    SaveRequest,
    SignupRequest,
    UpdateReaderRequest,
)
from app.api.schemas_base import UUID_PATTERN
from app.security.auth import check_password, create_reader_token, equalise_timing
from app.services.reader import (
    EmailAlreadyRegistered,
    ReaderError,
    ReaderService,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/readers", tags=["readers"])

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Incorrect email or password",
    headers={"WWW-Authenticate": "Bearer"},
)


def _conflict(exc: ReaderError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


# --- session ---------------------------------------------------------------


@router.post("/signup", response_model=ReaderTokenResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: SignupRequest,
    session: SessionDep,
    settings: SettingsDep,
    throttle: SignupThrottleDep,
    ip: ClientIpDep,
) -> ReaderTokenResponse:
    """Create an account and sign in with it.

    Returns a token rather than 201-and-go-log-in: the form that posts here is
    "build my feed", and bouncing someone to a login screen immediately after
    they typed their password is a step with no purpose.

    Throttled before the password is hashed, like ``/login``. Every attempt
    counts, taken or not: a successful signup is what a bot repeats.
    """
    retry_after = throttle.retry_after(ip=ip, email=payload.email)
    if retry_after is not None:
        raise too_many_attempts(retry_after, "Too many attempts. Try again later.")
    throttle.record_failure(ip=ip, email=payload.email)

    try:
        reader = await ReaderService(session).signup(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            interests=payload.interests,
            newsletter=payload.newsletter,
        )
    except EmailAlreadyRegistered as exc:
        # 409 with a plain message, unlike login's deliberately-generic 401.
        # Signup cannot hide that an address is taken — the endpoint's whole
        # job is to say so — and pretending otherwise just produces an account
        # the reader cannot use and cannot diagnose.
        raise _conflict(exc) from None

    token, expires_in = create_reader_token(reader.id, settings.jwt_secret)
    return ReaderTokenResponse(access_token=token, expires_in=expires_in)


@router.post("/login", response_model=ReaderTokenResponse)
async def login(
    payload: ReaderLoginRequest,
    session: SessionDep,
    settings: SettingsDep,
    throttle: ReaderLoginThrottleDep,
    ip: ClientIpDep,
) -> ReaderTokenResponse:
    email = payload.email

    retry_after = throttle.retry_after(ip=ip, email=email)
    if retry_after is not None:
        raise too_many_attempts(retry_after, "Too many attempts. Try again later.")

    reader = await ReaderService(session).by_email(email)

    if reader is None:
        await equalise_timing()
        throttle.record_failure(ip=ip, email=email)
        raise _INVALID_CREDENTIALS

    if not await check_password(payload.password, reader.password_hash):
        throttle.record_failure(ip=ip, email=email)
        raise _INVALID_CREDENTIALS

    throttle.record_success(ip=ip, email=email)
    token, expires_in = create_reader_token(reader.id, settings.jwt_secret)
    return ReaderTokenResponse(access_token=token, expires_in=expires_in)


# --- profile ---------------------------------------------------------------


@router.get("/me", response_model=ReaderOut)
async def me(reader: ReaderDep, session: SessionDep) -> ReaderOut:
    """The caller's own account. Also how the client re-validates a stored token."""
    row = await ReaderService(session).get(reader.id)
    return ReaderOut(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        interests=list(row.interests),
        newsletter=row.newsletter,
    )


@router.patch("/me", response_model=ReaderOut)
async def update_me(
    payload: UpdateReaderRequest, reader: ReaderDep, session: SessionDep
) -> ReaderOut:
    row = await ReaderService(session).update_profile(
        reader.id,
        display_name=payload.display_name,
        interests=payload.interests,
        newsletter=payload.newsletter,
    )
    return ReaderOut(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        interests=list(row.interests),
        newsletter=row.newsletter,
    )


# --- your data ------------------------------------------------------------
#
# The two routes a privacy notice promises: a copy of what we hold, and
# erasure. Both act on the caller's own row only — the subject comes from the
# token, never from the path — so neither can be pointed at someone else.


@router.get("/me/export")
async def export_me(reader: ReaderDep, session: SessionDep) -> JSONResponse:
    """A machine-readable copy of the account, as a file download.

    ``Content-Disposition: attachment`` so the browser saves it rather than
    rendering it, and ``no-store`` so no cache between here and the reader
    keeps a copy of someone's account data.
    """
    data = await ReaderService(session).export(reader.id)
    return JSONResponse(
        content=jsonable_encoder({"format": "you.th-reader-export/1", **data}),
        headers={
            "Content-Disposition": 'attachment; filename="youth-account-data.json"',
            "Cache-Control": "no-store",
        },
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    payload: DeleteAccountRequest,
    reader: ReaderDep,
    session: SessionDep,
    throttle: ReaderLoginThrottleDep,
    ip: ClientIpDep,
) -> None:
    """Erase the caller's account after re-checking their password.

    The password check goes through the **reader login throttle**, keyed the
    same way, before any hashing. Without it, a stolen token would turn this
    route into an unthrottled password oracle for the account it belongs to —
    the one thing ``/login`` is built not to be.
    """
    retry_after = throttle.retry_after(ip=ip, email=reader.email)
    if retry_after is not None:
        raise too_many_attempts(retry_after, "Too many attempts. Try again later.")

    service = ReaderService(session)
    row = await service.get(reader.id)
    if not await check_password(payload.password, row.password_hash):
        throttle.record_failure(ip=ip, email=reader.email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="That password is not right."
        )

    throttle.record_success(ip=ip, email=reader.email)
    await service.delete_account(reader.id)


# --- folders and saves -----------------------------------------------------


@router.get("/folders", response_model=list[FolderOut])
async def folders(reader: ReaderDep, session: SessionDep) -> list[FolderOut]:
    rows = await ReaderService(session).folders(reader.id)
    return [FolderOut(**row) for row in rows]


@router.post("/folders", response_model=FolderOut, status_code=status.HTTP_201_CREATED)
async def create_folder(
    payload: CreateFolderRequest, reader: ReaderDep, session: SessionDep
) -> FolderOut:
    service = ReaderService(session)
    try:
        folder = await service.create_folder(reader.id, payload.name)
    except ReaderError as exc:
        raise _conflict(exc) from None
    return FolderOut(id=folder.id, name=folder.name, count=0)


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    folder_id: IdPath, reader: ReaderDep, session: SessionDep
) -> None:
    try:
        await ReaderService(session).delete_folder(reader.id, folder_id)
    except ReaderError as exc:
        raise _conflict(exc) from None


@router.get("/saved", response_model=SavedPageOut)
async def saved(
    reader: ReaderDep,
    session: SessionDep,
    folder_id: Annotated[str | None, Query(alias="folderId", pattern=UUID_PATTERN)] = None,
) -> SavedPageOut:
    """The reader's shelf, plus the folder tabs above it, in one request."""
    service = ReaderService(session)
    return SavedPageOut(
        folders=[FolderOut(**row) for row in await service.folders(reader.id)],
        items=await service.saved_cards(reader.id, folder_id),  # type: ignore[arg-type]
    )


@router.get("/saved/slugs", response_model=dict[str, str])
async def saved_slugs(reader: ReaderDep, session: SessionDep) -> dict[str, str]:
    """``{slug: folderId}``, so a page of tiles knows its own state at once.

    Deliberately not merged into the feed response: the feed is public,
    cacheable and identical for everyone, and folding a per-reader field into
    it would make it neither.
    """
    return await ReaderService(session).saved_slugs(reader.id)


@router.put("/saved/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def save_article(
    slug: str, payload: SaveRequest, reader: ReaderDep, session: SessionDep
) -> None:
    """Put a published article on a shelf. Idempotent; re-saving moves it."""
    try:
        await ReaderService(session).save(reader.id, slug, payload.folder_id)
    except ReaderError as exc:
        raise _conflict(exc) from None


@router.delete("/saved/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def unsave_article(slug: str, reader: ReaderDep, session: SessionDep) -> None:
    try:
        await ReaderService(session).unsave(reader.id, slug)
    except ReaderError as exc:
        raise _conflict(exc) from None
