"""
JWT, password, cookie, and DB-backed session authentication utilities.
"""
from datetime import datetime, timedelta
import hashlib
import hmac
import re
from ipaddress import ip_address, ip_network
from typing import Optional

import bcrypt
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, Response, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_

from .config import get_settings
from .time_utils import utcnow
from .database import get_db
from .models import User, AuthSession

settings = get_settings()
security = HTTPBearer(auto_error=False)
BCRYPT_ROUNDS = 10
DUMMY_PASSWORD_HASH = "$2b$10$3jiR1K7OwHqIOyArZk1Mve71tFeDBhJYS2dhzTLbIEjGqmDVUehAC"


def normalize_password_secret(password: str) -> str:
    """Remove all whitespace from passwords without changing case.

    The frontend does the same while typing, but auth must never rely on the
    client. Bcrypt still receives the exact case-sensitive password after
    whitespace is stripped.
    """
    return re.sub(r"\s+", "", password or "")


def _password_bytes(password: str) -> bytes:
    normalized = normalize_password_secret(password)
    encoded = normalized.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError("Password is too long for bcrypt; use 72 bytes or fewer")
    return encoded


def hash_password(password: str) -> str:
    """Hash a password with bcrypt cost/rounds 10."""
    return bcrypt.hashpw(
        _password_bytes(password),
        bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
    ).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(_password_bytes(password), password_hash.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def token_hash(token: str) -> str:
    """Hash refresh tokens before storing them in the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_ip(ip: str | None) -> str | None:
    """Store a keyed IP hash instead of raw IP addresses."""
    if not ip:
        return None
    secret = settings.auth_session_ip_hash_secret or settings.jwt_secret
    return hmac.new(secret.encode("utf-8"), ip.encode("utf-8"), hashlib.sha256).hexdigest()


def _ip_matches_trusted_proxy(ip: str | None) -> bool:
    """Return true only for direct peers that are explicitly trusted proxies."""
    if not ip:
        return False
    trusted_values = settings.trusted_proxy_ips
    if not trusted_values:
        return False
    try:
        remote_ip = ip_address(ip)
    except ValueError:
        return False

    for value in trusted_values:
        try:
            if "/" in value:
                if remote_ip in ip_network(value, strict=False):
                    return True
            elif remote_ip == ip_address(value):
                return True
        except ValueError:
            continue
    return False


def request_ip(request: Request | None) -> str | None:
    """Return the client IP without trusting spoofed forwarding headers.

    X-Forwarded-For is only honored when the direct peer is a configured
    trusted proxy. This keeps rate limits and session IP hashes meaningful even
    when attackers send fake X-Forwarded-For headers themselves.
    """
    if not request:
        return None

    direct_ip = request.client.host if request.client else None
    if _ip_matches_trusted_proxy(direct_ip):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first_ip = forwarded.split(",", 1)[0].strip()
            if first_ip:
                return first_ip

    return direct_ip


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """Set HttpOnly cookies for browser sessions."""
    cookie_kwargs = {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "domain": settings.cookie_domain,
        "path": "/",
    }
    response.set_cookie(
        settings.session_access_cookie_name,
        access_token,
        max_age=int(settings.jwt_expiry_minutes * 60),
        **cookie_kwargs,
    )
    response.set_cookie(
        settings.session_refresh_cookie_name,
        refresh_token,
        max_age=int(settings.jwt_refresh_expiry_days * 24 * 60 * 60),
        **cookie_kwargs,
    )


def clear_auth_cookies(response: Response) -> None:
    """Clear HttpOnly auth cookies."""
    cookie_kwargs = {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "domain": settings.cookie_domain,
        "path": "/",
    }
    response.delete_cookie(settings.session_access_cookie_name, **cookie_kwargs)
    response.delete_cookie(settings.session_refresh_cookie_name, **cookie_kwargs)


def create_access_token(telegram_id: int, version: int = 0, session_id: str | None = None) -> str:
    """Create a JWT access token."""
    expire = utcnow() + timedelta(minutes=settings.jwt_expiry_minutes)
    payload = {
        "sub": str(telegram_id),
        "exp": expire,
        "type": "access",
        "ver": version,
    }
    if session_id:
        payload["sid"] = session_id
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_refresh_token(telegram_id: int, version: int = 0, session_id: str | None = None) -> str:
    """Create a JWT refresh token."""
    expire = utcnow() + timedelta(days=settings.jwt_refresh_expiry_days)
    payload = {
        "sub": str(telegram_id),
        "exp": expire,
        "type": "refresh",
        "ver": version,
    }
    if session_id:
        payload["sid"] = session_id
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_token_payload(token: str, token_type: str = "access") -> Optional[dict]:
    """Verify JWT token and return full payload if valid."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        if payload.get("type") != token_type:
            return None
        return payload
    except (JWTError, ValueError):
        return None


def verify_token(token: str, token_type: str = "access") -> Optional[int]:
    """Verify JWT token and return telegram_id if valid."""
    payload = verify_token_payload(token, token_type)
    if not payload:
        return None
    sub = payload.get("sub")
    return int(sub) if sub is not None else None


def get_request_token(
    request: Request | None,
    credentials: Optional[HTTPAuthorizationCredentials] = None,
    *,
    allow_query_token: bool = False,
) -> str | None:
    """Extract Bearer/cookie auth, with query tokens only when explicitly allowed."""
    if credentials:
        return credentials.credentials
    if request:
        cookie_token = request.cookies.get(settings.session_access_cookie_name)
        if cookie_token:
            return cookie_token
        if allow_query_token and "token" in request.query_params:
            return request.query_params["token"]
    return None


async def get_current_user(
    request: Request = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve current authenticated user.

    Supports Bearer tokens for Android/TV and HttpOnly cookies for web.
    Query-string tokens are disabled by default and only work when a specific
    endpoint explicitly opts in.
    """
    token = get_request_token(request, credentials)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = verify_token_payload(token, "access")
    telegram_id = int(payload.get("sub")) if payload and payload.get("sub") else None
    token_version = payload.get("ver") if payload else None
    session_id = payload.get("sid") if payload else None

    if not telegram_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    if token_version is not None and token_version < user.auth_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been invalidated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # New web/app sessions include sid and are validated against the DB so logout,
    # logout-all, refresh rotation, and temporary one-time-login expiry work.
    # Old bearer tokens without sid remain accepted until they expire for
    # Android/TV compatibility.
    if session_id:
        now = utcnow()
        temp_stale_after = now - timedelta(seconds=settings.temp_session_timeout_seconds)
        type_conditions = [
            (AuthSession.session_type == "temporary")
            & (AuthSession.last_seen_at > temp_stale_after)
        ]
        if settings.auth_session_inactive_days > 0:
            persistent_stale_after = now - timedelta(days=settings.auth_session_inactive_days)
            type_conditions.append(
                (AuthSession.session_type != "temporary")
                & (AuthSession.last_seen_at > persistent_stale_after)
            )
        else:
            type_conditions.append(AuthSession.session_type != "temporary")

        session_result = await db.execute(
            select(AuthSession).where(
                AuthSession.session_id == session_id,
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
                or_(*type_conditions),
            )
        )
        session = session_result.scalar_one_or_none()
        if not session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session is no longer active",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return user
