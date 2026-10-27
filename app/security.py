"""Passwords, sessions, secret encryption, rate limiting and SSRF guards."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import socket
import threading
import time
from urllib.parse import urlparse

import jwt
from cryptography.fernet import Fernet, InvalidToken

from app import config, db

SESSION_COOKIE = "sahm_session"
SESSION_TTL = 60 * 60 * 24 * 14  # 14 days

# ---- passwords (scrypt, stdlib) ------------------------------------------
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    if len(password) < 8:
        return "password_too_short"
    if len(password) > 200:
        return "password_too_long"
    return None


# ---- sessions (JWT in an httpOnly cookie) --------------------------------

def _signing_key() -> str:
    key = config.settings.secret_key
    if not key:
        # Development fallback: stable per data directory, never used in production.
        path = config.settings.data_dir / ".dev_secret"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(secrets.token_urlsafe(48))
        key = path.read_text().strip()
    return key


def issue_session(user: dict) -> str:
    now = int(time.time())
    payload = {"sub": user["id"], "tv": user["token_version"], "iat": now, "exp": now + SESSION_TTL}
    return jwt.encode(payload, _signing_key(), algorithm="HS256")


def read_session(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, _signing_key(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    user = db.one("SELECT * FROM users WHERE id = ?", (payload.get("sub"),))
    if not user or user["token_version"] != payload.get("tv"):
        return None
    return user


# ---- encryption of third-party secrets (tokens, API keys) ---------------

def _fernet() -> Fernet:
    key = config.settings.encryption_key
    if not key:
        path = config.settings.data_dir / ".dev_encryption_key"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(Fernet.generate_key().decode())
        key = path.read_text().strip()
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return ""


def random_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


# ---- rate limiting ---------------------------------------------------------

class RateLimiter:
    """In-process sliding window limiter (per key)."""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < window]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 50_000:  # bound memory
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < window}
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def login_allowed(key: str, limit: int = 8, window: int = 900) -> bool:
    """Persistent login throttle so restarts do not reset it."""
    cutoff = db.now() - window
    db.run("DELETE FROM login_attempts WHERE at < ?", (cutoff,))
    count = db.one("SELECT COUNT(*) AS n FROM login_attempts WHERE key = ? AND at >= ?", (key, cutoff))["n"]
    return count < limit


def record_failed_login(key: str) -> None:
    db.insert("login_attempts", {"key": key, "at": db.now()})


# ---- outbound URL safety (SSRF) -----------------------------------------

class UnsafeURL(ValueError):
    pass


def check_public_url(url: str) -> str:
    """Allow only http(s) URLs that resolve to public addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeURL("only http(s) URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeURL("credentials in URL are not allowed")
    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UnsafeURL(f"cannot resolve host {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified
        ):
            raise UnsafeURL("private or internal addresses are not allowed")
    return url
