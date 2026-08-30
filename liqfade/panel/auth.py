"""Single password cookie session auth for the panel.

The real access control is the network boundary: the panel binds loopback and is reached
over Tailscale or an SSH tunnel. The password stops a second person on the same box and
a forgotten tunnel, it is not the only thing between the internet and the account.

bcrypt rather than argon2: it is named in CLAUDE.md, it is one small wheel with no build
step on the VPS, and against a single high entropy password behind a private network its
weaker memory hardness does not change the outcome. See docs/DECISIONS.md.
"""

from __future__ import annotations

import getpass
import os
import secrets
import time
from dataclasses import dataclass, field

import bcrypt
from itsdangerous import BadSignature, URLSafeTimedSerializer

HASH_ENV = "LIQFADE_PANEL_PASSWORD_HASH"
SECRET_ENV = "LIQFADE_PANEL_SESSION_SECRET"  # noqa: S105
COOKIE_NAME = "liqfade_session"


class AuthNotConfiguredError(RuntimeError):
    """Raised when LIQFADE_PANEL_PASSWORD_HASH is missing. The panel refuses to start."""


def hash_password(password: str) -> str:
    """Return a bcrypt hash for LIQFADE_PANEL_PASSWORD_HASH."""
    if len(password) < 12:
        raise ValueError("panel password must be at least 12 characters")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def hash_password_interactive() -> str:
    """Prompt twice on a tty and print the hash. Never echoes and never logs."""
    first = getpass.getpass("panel password: ")
    second = getpass.getpass("repeat: ")
    if first != second:
        raise ValueError("passwords do not match")
    return hash_password(first)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        # A malformed hash in the env file must read as "wrong password", not crash.
        return False


@dataclass
class LoginLimiter:
    """Fixed window counter of failed logins, keyed by client address.

    Deliberately in memory: the panel is one process serving one operator, so a restart
    clearing the counters is not a meaningful bypass, and a persistent store would be
    more moving parts than the threat justifies.
    """

    max_attempts: int
    window_s: float
    _failures: dict[str, list[float]] = field(default_factory=dict)

    def _prune(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window_s]
        self._failures[key] = recent
        return recent

    def blocked(self, key: str, now: float | None = None) -> bool:
        return len(self._prune(key, now or time.monotonic())) >= self.max_attempts

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = now or time.monotonic()
        self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


class SessionAuth:
    """Signed, expiring session cookies over a single password."""

    def __init__(self, *, password_hash: str, secret: str, ttl_s: float) -> None:
        self.password_hash = password_hash
        self.ttl_s = ttl_s
        self._serializer = URLSafeTimedSerializer(secret, salt="liqfade-panel")

    @classmethod
    def from_env(cls, ttl_s: float) -> SessionAuth:
        password_hash = os.environ.get(HASH_ENV, "").strip()
        if not password_hash:
            raise AuthNotConfiguredError(
                f"{HASH_ENV} is not set. Generate one with `liqfade panel hash-password` "
                "and put it in the .env file the panel service reads."
            )
        # A generated secret means every restart invalidates existing sessions, which is
        # the safe default; set SECRET_ENV to keep sessions across restarts.
        secret = os.environ.get(SECRET_ENV) or secrets.token_urlsafe(32)
        return cls(password_hash=password_hash, secret=secret, ttl_s=ttl_s)

    def login(self, password: str) -> str | None:
        """Return a session token on success, None on failure."""
        if not verify_password(password, self.password_hash):
            return None
        return self._serializer.dumps({"iat": int(time.time())})

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            self._serializer.loads(token, max_age=int(self.ttl_s))
        except BadSignature:
            return False
        return True
