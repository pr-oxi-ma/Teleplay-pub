"""
Authentication API endpoints.
"""
from datetime import datetime, timedelta
import secrets
import string
import re

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update, or_

from ..database import get_db
from ..time_utils import utcnow
from ..models import User, LoginCode, WebCredential, AuthSession
from ..schemas import (
    Token,
    UserResponse,
    LoginCodeRequest,
    LoginCodeResponse,
    VerifyCodeRequest,
    AuthResponse,
    RefreshTokenRequest,
    PasswordLoginRequest,
    PasswordChangeRequest,
    BotInfoResponse,
    PollCodeResponse,
    AuthSessionResponse,
    UsernameCheckResponse,
    UsernameUpdateRequest,
    WebCredentialResponse,
    MessageResponse,
)
from ..auth import (
    create_access_token,
    create_refresh_token,
    verify_token_payload,
    get_current_user,
    set_auth_cookies,
    clear_auth_cookies,
    verify_password,
    normalize_password_secret,
    DUMMY_PASSWORD_HASH,
    token_hash,
    hash_ip,
    request_ip,
    get_request_token,
    security,
)
from ..telegram import tg_client
from ..config import get_settings
from ..rate_limit import limiter
from ..username_rules import RESERVED_USERNAMES, BANNED_USERNAME_PARTS

router = APIRouter(prefix="/auth", tags=["Authentication"])
settings = get_settings()
USERNAME_RE = re.compile(r"^[a-z](?:[a-z0-9._-]*[a-z0-9])?$")
INVALID_CREDENTIALS = "Invalid credentials"


def token_expires_in_seconds() -> int:
    return int(settings.jwt_expiry_minutes * 60)


def normalize_username(username: str) -> str:
    return re.sub(r"\s+", "", (username or "").lower())


def username_validation_error(username: str) -> str | None:
    username = normalize_username(username)
    min_len = settings.web_username_min_length
    max_len = settings.web_username_max_length
    if not (min_len <= len(username) <= max_len):
        return f"Username must be {min_len}-{max_len} characters"
    if not USERNAME_RE.fullmatch(username):
        return "Username can use lowercase letters, numbers, dot, underscore, or dash, must start with a letter, and must end with a letter or number"
    if ".." in username or "--" in username or "__" in username:
        return "Username cannot contain repeated dot, dash, or underscore"
    if username in RESERVED_USERNAMES:
        return "This username is reserved"
    if any(part in username for part in BANNED_USERNAME_PARTS):
        return "This username is not allowed"
    return None


def validate_web_username(username: str) -> str:
    username = normalize_username(username)
    reason = username_validation_error(username)
    if reason:
        raise HTTPException(status_code=400, detail=reason)
    return username


def validate_login_username(username: str) -> str:
    username = normalize_username(username)
    # Do not reveal username validation details during login. Invalid format,
    # missing user, and bad password all look the same to the client.
    if username_validation_error(username):
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)
    return username


def validate_password_for_storage(password: str) -> str:
    normalized = normalize_password_secret(password)
    if len(normalized) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if len(normalized.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="Password is too long for bcrypt; use 72 bytes or fewer")
    return normalized


def public_user(user: User) -> UserResponse:
    return UserResponse.model_validate(user, from_attributes=True)


def _session_response(session: AuthSession, current_session_id: str | None = None) -> AuthSessionResponse:
    return AuthSessionResponse(
        session_id=session.session_id,
        current=session.session_id == current_session_id,
        session_type=session.session_type,
        user_agent=session.user_agent,
        created_at=session.created_at,
        last_used_at=session.last_used_at,
        last_seen_at=session.last_seen_at,
        expires_at=session.expires_at,
    )


async def revoke_all_sessions_for_user(user: User, db: AsyncSession) -> None:
    user.auth_version += 1
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    db.add(user)


def temporary_session_cutoff(now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(seconds=settings.temp_session_timeout_seconds)


def active_session_conditions(now: datetime | None = None):
    now = now or utcnow()
    type_conditions = [
        (AuthSession.session_type == "temporary")
        & (AuthSession.last_seen_at > temporary_session_cutoff(now))
    ]
    if settings.auth_session_inactive_days > 0:
        persistent_cutoff = now - timedelta(days=settings.auth_session_inactive_days)
        type_conditions.append(
            (AuthSession.session_type != "temporary")
            & (AuthSession.last_seen_at > persistent_cutoff)
        )
    else:
        type_conditions.append(AuthSession.session_type != "temporary")

    return (
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > now,
        or_(*type_conditions),
    )


async def revoke_stale_temporary_sessions(db: AsyncSession, user_id: int | None = None) -> None:
    conditions = [
        AuthSession.session_type == "temporary",
        AuthSession.revoked_at.is_(None),
        AuthSession.last_seen_at <= temporary_session_cutoff(),
    ]
    if user_id is not None:
        conditions.append(AuthSession.user_id == user_id)
    await db.execute(
        update(AuthSession)
        .where(*conditions)
        .values(revoked_at=utcnow())
    )


async def cleanup_auth_sessions(
    db: AsyncSession,
    user_id: int | None = None,
    keep_session_id: str | None = None,
) -> None:
    """Prune abandoned DB-backed auth sessions without relying on users.

    Incognito/private-window logins lose their browser cookies when the window is
    closed, so the app will never receive a normal logout call for those rows.
    This cleanup is intentionally triggered from normal auth flows instead of a
    background worker so it works on Render/free hosts where scheduled workers
    may not be available.
    """
    if not settings.auth_session_cleanup_enabled:
        return

    now = utcnow()

    # Temporary one-time sessions should disappear quickly once heartbeats stop.
    await revoke_stale_temporary_sessions(db, user_id)

    # Persistent sessions abandoned by closed incognito tabs / old browsers are
    # not useful forever. Revoke them after a configurable inactivity window.
    if settings.auth_session_inactive_days > 0:
        inactive_cutoff = now - timedelta(days=settings.auth_session_inactive_days)
        inactive_conditions = [
            AuthSession.session_type == "persistent",
            AuthSession.revoked_at.is_(None),
            AuthSession.last_seen_at <= inactive_cutoff,
        ]
        if user_id is not None:
            inactive_conditions.append(AuthSession.user_id == user_id)
        await db.execute(
            update(AuthSession)
            .where(*inactive_conditions)
            .values(revoked_at=now)
        )

    # Hard-delete dead rows so auth_sessions cannot grow forever.
    delete_conditions = [AuthSession.expires_at <= now]
    if settings.auth_session_revoked_retention_days >= 0:
        revoked_cutoff = now - timedelta(days=settings.auth_session_revoked_retention_days)
        delete_conditions.append(
            (AuthSession.revoked_at.is_not(None))
            & (AuthSession.revoked_at <= revoked_cutoff)
        )
    await db.execute(delete(AuthSession).where(or_(*delete_conditions)))

    # Cap active sessions per user. This directly handles the lazy-user case:
    # Chrome + Firefox + Brave + incognito + phone + laptop should not leave an
    # unlimited number of usable sessions in the DB.
    max_active = settings.auth_session_max_active_per_user
    if user_id is None or max_active <= 0:
        return

    result = await db.execute(
        select(AuthSession.session_id)
        .where(AuthSession.user_id == user_id, *active_session_conditions(now))
        .order_by(
            AuthSession.last_seen_at.desc(),
            AuthSession.last_used_at.desc(),
            AuthSession.created_at.desc(),
        )
    )
    active_session_ids = [row[0] for row in result.all()]

    keepers: list[str] = []
    if keep_session_id and keep_session_id in active_session_ids:
        keepers.append(keep_session_id)
        active_session_ids.remove(keep_session_id)

    keepers.extend(active_session_ids[: max(0, max_active - len(keepers))])
    revoke_session_ids = [session_id for session_id in active_session_ids if session_id not in set(keepers)]

    if revoke_session_ids:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.session_id.in_(revoke_session_ids), AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )


async def current_session_from_request(
    request: Request,
    credentials,
    current_user: User,
    db: AsyncSession,
) -> AuthSession:
    """Return the active DB session for the current access token."""
    token = get_request_token(request, credentials, allow_query_token=False)
    payload = verify_token_payload(token or "", "access")
    current_sid = payload.get("sid") if payload else None
    if not current_sid:
        raise HTTPException(status_code=400, detail="Current session is not session-backed")

    now = utcnow()
    result = await db.execute(
        select(AuthSession).where(
            AuthSession.user_id == current_user.id,
            AuthSession.session_id == current_sid,
            *active_session_conditions(now),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=401, detail="Current session is no longer active")
    return session


def ensure_temporary_session_can_manage_only_itself(current_session: AuthSession, target_session_id: str | None = None) -> None:
    """Temporary one-time-login sessions may revoke only themselves.

    A /web or code-login session is intentionally short-lived. It should not be
    allowed to clean up persistent username/password sessions from Settings.
    """
    if current_session.session_type != "temporary":
        return
    if target_session_id and target_session_id == current_session.session_id:
        return
    raise HTTPException(
        status_code=403,
        detail="One-time login sessions can only revoke themselves. Sign in with username/password to manage other sessions.",
    )


async def create_session_tokens(user: User, request: Request | None, db: AsyncSession, session_type: str = "persistent") -> tuple[str, str, AuthSession]:
    """Create DB-backed session, access token, and refresh token."""
    now = utcnow()
    expires_at = now + timedelta(days=settings.jwt_refresh_expiry_days)

    for _ in range(10):
        session_id = secrets.token_urlsafe(32)[:64]
        existing = await db.execute(select(AuthSession.id).where(AuthSession.session_id == session_id))
        if existing.scalar_one_or_none() is None:
            break
    else:
        raise HTTPException(status_code=500, detail="Could not create a unique session")

    access_token = create_access_token(user.telegram_id, version=user.auth_version, session_id=session_id)
    refresh_token = create_refresh_token(user.telegram_id, version=user.auth_version, session_id=session_id)
    normalized_session_type = "temporary" if session_type == "temporary" else "persistent"
    session = AuthSession(
        session_id=session_id,
        user_id=user.id,
        telegram_id=user.telegram_id,
        refresh_token_hash=token_hash(refresh_token),
        user_agent=(request.headers.get("user-agent", "")[:500] if request else None),
        ip_hash=hash_ip(request_ip(request)),
        created_at=now,
        last_used_at=now,
        last_seen_at=now,
        session_type=normalized_session_type,
        expires_at=expires_at,
        revoked_at=None,
    )
    db.add(session)
    await db.flush()
    await cleanup_auth_sessions(db, user_id=user.id, keep_session_id=session.session_id)
    return access_token, refresh_token, session


async def issue_auth_response(user: User, response: Response, request: Request | None, db: AsyncSession, session_type: str = "persistent") -> AuthResponse:
    """Create DB-backed tokens, set HttpOnly cookies for web, and return tokens for legacy clients."""
    access_token, refresh_token, _ = await create_session_tokens(user, request, db, session_type=session_type)
    set_auth_cookies(response, access_token, refresh_token)
    await db.commit()
    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=token_expires_in_seconds(),
        user=public_user(user),
    )


async def user_for_login_code(
    entered_code: str,
    db: AsyncSession,
    *,
    require_claimed: bool = True,
) -> tuple[User, LoginCode | None]:
    """Resolve a normal one-time code into a user.

    Master/global login codes were intentionally removed. Permanent web
    username/password credentials and Telegram-owned one-time login links are
    now the only web authentication flows.
    """
    entered_code = entered_code.strip().upper()

    result = await db.execute(select(LoginCode).where(LoginCode.code == entered_code))
    login_code = result.scalar_one_or_none()

    if not login_code:
        raise HTTPException(status_code=400, detail="Invalid login code")

    if login_code.expires_at < utcnow():
        await db.delete(login_code)
        await db.commit()
        raise HTTPException(status_code=410, detail="Login code expired")

    if require_claimed and not login_code.telegram_id:
        raise HTTPException(status_code=404, detail="Code not yet verified")

    if not login_code.telegram_id:
        raise HTTPException(status_code=404, detail="Code not linked to a Telegram user")

    result = await db.execute(select(User).where(User.telegram_id == login_code.telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user, login_code


@router.get("/bot/info", response_model=BotInfoResponse)
async def get_bot_info_endpoint():
    """Get bot username and name for the login screen."""
    try:
        me = await tg_client.get_me()
        return BotInfoResponse(username=me.username, name=f"{me.first_name} {me.last_name or ''}".strip(), server_version="1.0.0")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def password_login_impl(
    request: Request,
    response: Response,
    credentials: PasswordLoginRequest,
    db: AsyncSession,
) -> AuthResponse:
    """Login with bot-created permanent username/password credentials."""
    username = validate_login_username(credentials.username)
    result = await db.execute(select(WebCredential).where(WebCredential.username == username))
    credential = result.scalar_one_or_none()

    password_hash = credential.password_hash if credential else DUMMY_PASSWORD_HASH
    password_ok = verify_password(credentials.password, password_hash)
    if not credential or not password_ok:
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)

    result = await db.execute(select(User).where(User.id == credential.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)

    return await issue_auth_response(user, response, request, db)


@router.post("/password/login", response_model=AuthResponse)
@limiter.limit("5/minute")
async def login_with_password_route(
    request: Request,
    response: Response,
    credentials: PasswordLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    return await password_login_impl(request, response, credentials, db)


# Backward-compatible alias used by older web builds.
@router.post("/login", response_model=AuthResponse)
@limiter.limit("5/minute")
async def login_with_password_alias(
    request: Request,
    response: Response,
    credentials: PasswordLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    return await password_login_impl(request, response, credentials, db)


@router.post("/password/change", response_model=MessageResponse)
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    response: Response,
    payload: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the permanent web password, then revoke all existing sessions."""
    validate_password_for_storage(payload.new_password)
    result = await db.execute(select(WebCredential).where(WebCredential.user_id == current_user.id))
    credential = result.scalar_one_or_none()
    if not credential or not verify_password(payload.current_password, credential.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    from ..auth import hash_password

    credential.password_hash = hash_password(payload.new_password)
    credential.telegram_id = current_user.telegram_id
    credential.updated_at = utcnow()
    db.add(credential)
    await revoke_all_sessions_for_user(current_user, db)
    await db.commit()
    clear_auth_cookies(response)
    return MessageResponse(message="Password changed. All old sessions were revoked; please sign in again.")


@router.get("/web-credential", response_model=WebCredentialResponse)
async def get_web_credential(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(WebCredential).where(WebCredential.user_id == current_user.id))
    credential = result.scalar_one_or_none()
    return WebCredentialResponse(
        username=credential.username if credential else None,
        has_password=credential is not None,
    )


@router.get("/username/check", response_model=UsernameCheckResponse)
@limiter.limit("30/minute")
async def check_username_availability(
    request: Request,
    username: str = Query(..., min_length=1, max_length=255),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    normalized = normalize_username(username)
    reason = username_validation_error(normalized)
    if reason:
        return UsernameCheckResponse(username=normalized, available=False, valid=False, reason=reason)

    result = await db.execute(select(WebCredential).where(WebCredential.username == normalized))
    credential = result.scalar_one_or_none()
    if credential and credential.user_id != current_user.id:
        return UsernameCheckResponse(username=normalized, available=False, valid=True, reason="Username is already taken")

    return UsernameCheckResponse(username=normalized, available=True, valid=True)


@router.patch("/username", response_model=MessageResponse)
@limiter.limit("10/minute")
async def change_username(
    request: Request,
    payload: UsernameUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the permanent web username from the web app only."""
    username = validate_web_username(payload.username)
    result = await db.execute(select(WebCredential).where(WebCredential.user_id == current_user.id))
    credential = result.scalar_one_or_none()
    if not credential:
        raise HTTPException(status_code=404, detail="Create web credentials in Telegram first with /setlogin")

    result = await db.execute(select(WebCredential).where(WebCredential.username == username))
    existing = result.scalar_one_or_none()
    if existing and existing.user_id != current_user.id:
        raise HTTPException(status_code=409, detail="Username is already taken")

    credential.username = username
    credential.updated_at = utcnow()
    db.add(credential)
    await db.commit()
    return MessageResponse(message="Username updated")


@router.post("/refresh", response_model=Token)
@limiter.limit("20/minute")
async def refresh_token(
    request: Request,
    response: Response,
    payload: RefreshTokenRequest | None = Body(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Refresh access token using body refresh token or HttpOnly refresh cookie. Rotates refresh tokens."""
    refresh_token_value = None
    if payload and payload.refresh_token:
        refresh_token_value = payload.refresh_token
    if not refresh_token_value:
        refresh_token_value = request.cookies.get(settings.session_refresh_cookie_name)

    payload_data = verify_token_payload(refresh_token_value or "", token_type="refresh")
    telegram_id = int(payload_data.get("sub")) if payload_data and payload_data.get("sub") else None
    token_version = payload_data.get("ver") if payload_data else None
    session_id = payload_data.get("sid") if payload_data else None

    if not telegram_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if token_version is not None and token_version < user.auth_version:
        raise HTTPException(status_code=401, detail="Refresh token has been invalidated")

    now = utcnow()
    session = None
    if session_id:
        result = await db.execute(
            select(AuthSession).where(
                AuthSession.session_id == session_id,
                AuthSession.user_id == user.id,
                *active_session_conditions(now),
            )
        )
        session = result.scalar_one_or_none()
        if not session or not secrets.compare_digest(session.refresh_token_hash, token_hash(refresh_token_value or "")):
            raise HTTPException(status_code=401, detail="Refresh session is no longer valid")

        new_access_token = create_access_token(telegram_id, version=user.auth_version, session_id=session.session_id)
        new_refresh_token = create_refresh_token(telegram_id, version=user.auth_version, session_id=session.session_id)
        session.refresh_token_hash = token_hash(new_refresh_token)
        session.last_used_at = now
        session.last_seen_at = now
        session.user_agent = request.headers.get("user-agent", "")[:500]
        session.ip_hash = hash_ip(request_ip(request))
        db.add(session)
        await cleanup_auth_sessions(db, user_id=user.id, keep_session_id=session.session_id)
        await db.commit()
    else:
        # Backward-compatible path for older refresh tokens without sid.
        new_access_token, new_refresh_token, _ = await create_session_tokens(user, request, db)
        await db.commit()

    set_auth_cookies(response, new_access_token, new_refresh_token)
    return Token(access_token=new_access_token, refresh_token=new_refresh_token, expires_in=token_expires_in_seconds())


@router.post("/logout")
async def logout_current_session(
    request: Request,
    response: Response,
    credentials=Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Revoke the current DB-backed session and clear browser cookies."""
    token = get_request_token(request, credentials, allow_query_token=False)
    payload = verify_token_payload(token or "", "access")
    session_id = payload.get("sid") if payload else None
    if session_id:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.session_id == session_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        await db.commit()
    clear_auth_cookies(response)
    return {"message": "Logged out."}


@router.post("/logout-all")
async def logout_all(
    request: Request,
    response: Response,
    credentials=Depends(security),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Invalidate all active sessions for the current user.

    Temporary one-time-login sessions are not allowed to revoke persistent
    sessions. They can only revoke themselves through /logout,
    /auth/session/close, or DELETE /auth/sessions/{current_session_id}.
    """
    current_session = await current_session_from_request(request, credentials, current_user, db)
    ensure_temporary_session_can_manage_only_itself(current_session)

    current_user.auth_version += 1
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == current_user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    db.add(current_user)
    await db.commit()
    clear_auth_cookies(response)
    return {"message": "All sessions have been invalidated"}


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    return public_user(current_user)


@router.get("/sessions", response_model=list[AuthSessionResponse])
async def list_auth_sessions(
    request: Request,
    credentials=Depends(security),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List active sessions for the current user."""
    token = get_request_token(request, credentials, allow_query_token=False)
    payload = verify_token_payload(token or "", "access")
    current_sid = payload.get("sid") if payload else None
    now = utcnow()
    await cleanup_auth_sessions(db, user_id=current_user.id, keep_session_id=current_sid)
    await db.commit()
    now = utcnow()
    result = await db.execute(
        select(AuthSession)
        .where(AuthSession.user_id == current_user.id, *active_session_conditions(now))
        .order_by(AuthSession.last_used_at.desc())
    )
    return [_session_response(session, current_sid) for session in result.scalars().all()]


@router.delete("/sessions")
async def revoke_other_auth_sessions(
    request: Request,
    credentials=Depends(security),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke all active sessions except the current one."""
    current_session = await current_session_from_request(request, credentials, current_user, db)
    ensure_temporary_session_can_manage_only_itself(current_session)

    result = await db.execute(
        update(AuthSession)
        .where(
            AuthSession.user_id == current_user.id,
            AuthSession.session_id != current_session.session_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    await db.commit()
    return {"message": f"Revoked {result.rowcount or 0} other session(s)"}


@router.delete("/sessions/{session_id}")
async def revoke_auth_session(
    session_id: str,
    request: Request,
    credentials=Depends(security),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke one active session belonging to the current user."""
    current_session = await current_session_from_request(request, credentials, current_user, db)
    ensure_temporary_session_can_manage_only_itself(current_session, session_id)

    result = await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == current_user.id, AuthSession.session_id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"message": "Session revoked"}


@router.post("/session/heartbeat", response_model=MessageResponse)
@limiter.limit("30/minute")
async def heartbeat_current_session(
    request: Request,
    credentials=Depends(security),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Keep the current session alive.

    Temporary sessions created from one-time code/link login are revoked
    automatically if this heartbeat stops for TEMP_SESSION_TIMEOUT_SECONDS.
    Persistent username/password sessions can call this endpoint too; it only
    updates last activity metadata.
    """
    token = get_request_token(request, credentials, allow_query_token=False)
    payload = verify_token_payload(token or "", "access")
    session_id = payload.get("sid") if payload else None
    if not session_id:
        raise HTTPException(status_code=400, detail="Current session is not session-backed")

    now = utcnow()
    result = await db.execute(
        select(AuthSession).where(
            AuthSession.session_id == session_id,
            AuthSession.user_id == current_user.id,
            *active_session_conditions(now),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=401, detail="Session is no longer active")

    session.last_seen_at = now
    session.last_used_at = now
    session.user_agent = request.headers.get("user-agent", "")[:500]
    session.ip_hash = hash_ip(request_ip(request))
    db.add(session)
    await db.commit()
    return MessageResponse(message="Heartbeat received")


@router.post("/session/close", response_model=MessageResponse)
@limiter.limit("20/minute")
async def close_temporary_session(
    request: Request,
    credentials=Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Best-effort tab/browser close hook.

    Only temporary one-time-login sessions are revoked here. Persistent
    username/password sessions are left alone, so a page unload cannot log out
    normal password-login users.
    """
    token = get_request_token(request, credentials, allow_query_token=False)
    payload = verify_token_payload(token or "", "access")
    session_id = payload.get("sid") if payload else None
    if not session_id:
        return MessageResponse(message="No session to close")

    await db.execute(
        update(AuthSession)
        .where(
            AuthSession.session_id == session_id,
            AuthSession.session_type == "temporary",
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    await db.commit()
    return MessageResponse(message="Temporary session closed")


@router.post("/code/generate", response_model=LoginCodeResponse)
@router.post("/generate-code", response_model=LoginCodeResponse)
@limiter.limit("6/minute")
async def generate_login_code(request: Request, db: AsyncSession = Depends(get_db)):
    """Generate a short one-time login code for TV/Web polling authentication."""
    now = utcnow()
    await db.execute(delete(LoginCode).where(LoginCode.expires_at < now))

    alphabet = string.ascii_uppercase + string.digits
    for _ in range(10):
        code = "".join(secrets.choice(alphabet) for _ in range(settings.login_code_length))
        existing = await db.execute(select(LoginCode.id).where(LoginCode.code == code))
        if existing.scalar_one_or_none() is None:
            break
    else:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Could not generate a unique login code")

    expires_at = now + timedelta(minutes=settings.login_code_expiry_minutes)
    login_code = LoginCode(code=code, telegram_id=None, expires_at=expires_at)
    db.add(login_code)
    await db.commit()
    await db.refresh(login_code)
    return LoginCodeResponse(code=code, expires_at=expires_at)


@router.post("/code/poll", response_model=PollCodeResponse)
@router.post("/poll-code", response_model=PollCodeResponse)
@limiter.limit("30/minute")
async def poll_login_code(
    request: Request,
    response: Response,
    code_request: VerifyCodeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Poll a code without generating 404 log spam while it is still pending."""
    entered_code = code_request.code.strip().upper()
    result = await db.execute(select(LoginCode).where(LoginCode.code == entered_code))
    login_code = result.scalar_one_or_none()
    if not login_code:
        raise HTTPException(status_code=400, detail="Invalid login code")
    if login_code.expires_at < utcnow():
        await db.delete(login_code)
        await db.commit()
        raise HTTPException(status_code=410, detail="Login code expired")
    if not login_code.telegram_id:
        return PollCodeResponse(status="pending", message="Waiting for Telegram confirmation")

    user, login_code = await user_for_login_code(entered_code, db, require_claimed=True)
    if login_code:
        await db.delete(login_code)
    auth = await issue_auth_response(user, response, request, db, session_type="temporary")
    return PollCodeResponse(
        status="claimed",
        access_token=auth.access_token,
        refresh_token=auth.refresh_token,
        expires_in=auth.expires_in,
        user=auth.user,
    )


@router.post("/code/verify", response_model=AuthResponse)
@router.post("/verify-code", response_model=AuthResponse)
@limiter.limit("12/minute")
async def verify_login_code(
    request: Request,
    response: Response,
    code_request: VerifyCodeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Verify a bot-claimed code. On success the code is deleted and cookies are set."""
    user, login_code = await user_for_login_code(code_request.code, db, require_claimed=True)
    if login_code:
        await db.delete(login_code)
    return await issue_auth_response(user, response, request, db, session_type="temporary")


@router.post("/link/exchange", response_model=AuthResponse)
@router.post("/exchange-code", response_model=AuthResponse)
@limiter.limit("10/minute")
async def exchange_one_time_code(
    request: Request,
    response: Response,
    code_request: VerifyCodeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a one-time web-link code for an HttpOnly cookie session."""
    user, login_code = await user_for_login_code(code_request.code, db, require_claimed=True)
    if login_code:
        await db.delete(login_code)
    return await issue_auth_response(user, response, request, db, session_type="temporary")


# Backward compatible alias used by older web builds.
@router.post("/code", response_model=AuthResponse)
async def login_with_code(
    request: Request,
    response: Response,
    code_request: LoginCodeRequest,
    db: AsyncSession = Depends(get_db),
):
    return await verify_login_code(request, response, VerifyCodeRequest(code=code_request.code), db)
