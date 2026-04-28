import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from config import LOGIN_ATTEMPT_WINDOW_MINUTES, MAX_LOGIN_ATTEMPTS, SESSION_EXPIRY_HOURS
from database import _db

def _hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${password_hash}"


def _verify_password(password: str, stored_hash: str) -> bool:
    salt, _ = stored_hash.split("$", 1)
    return secrets.compare_digest(_hash_password(password, salt), stored_hash)


def _create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=SESSION_EXPIRY_HOURS)
    with _db() as connection:
        connection.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now.isoformat(), expires_at.isoformat()),
        )
    return token


def _user_from_token(token: Optional[str]) -> Optional[sqlite3.Row]:
    if not token:
        return None
    with _db() as connection:
        return connection.execute(
            """
            SELECT users.* FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ? AND sessions.expires_at > ?
            """,
            (token, datetime.now(timezone.utc).isoformat()),
        ).fetchone()


def _require_user(token: Optional[str]) -> sqlite3.Row:
    user = _user_from_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid. Please login again.")
    return user


def _check_rate_limit(email: str) -> bool:
    """Check if user has exceeded login attempt limit."""
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=LOGIN_ATTEMPT_WINDOW_MINUTES)
    with _db() as connection:
        count = connection.execute(
            "SELECT COUNT(*) as cnt FROM login_attempts WHERE email = ? AND attempted_at > ? AND success = 0",
            (email, cutoff_time.isoformat()),
        ).fetchone()
    return count["cnt"] < MAX_LOGIN_ATTEMPTS


def _record_login_attempt(email: str, success: bool = False) -> None:
    """Record a login attempt for rate limiting."""
    with _db() as connection:
        connection.execute(
            "INSERT INTO login_attempts (email, attempted_at, success) VALUES (?, ?, ?)",
            (email, datetime.now(timezone.utc).isoformat(), 1 if success else 0),
        )
