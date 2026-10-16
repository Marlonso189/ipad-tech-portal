"""In-memory sessions. The Jamf token never leaves this process."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Session:
    sid: str
    csrf_token: str
    created_at: float
    last_seen: float
    username: str | None = None
    token: str | None = None
    token_expires: float | None = None
    privileges: set[str] = field(default_factory=set)
    privilege_set: str = ""

    @property
    def authenticated(self) -> bool:
        return bool(self.username and self.token)


class SessionStore:
    def __init__(self, idle_timeout_seconds: int) -> None:
        self.idle_timeout_seconds = idle_timeout_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create_anonymous(self) -> Session:
        session = Session(
            sid=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            created_at=time.time(),
            last_seen=time.time(),
        )
        with self._lock:
            self._sweep_locked()
            self._sessions[session.sid] = session
        return session

    def lookup(self, sid: str | None) -> tuple[str, Session | None]:
        """Return missing, expired, or ok. Expired sessions are dropped."""
        if not sid:
            return "missing", None
        with self._lock:
            session = self._sessions.get(sid)
            if session is not None and self._expired(session):
                self._sessions.pop(sid, None)
                self._sweep_locked()
                return "expired", None
            self._sweep_locked()
            if session is None:
                return "missing", None
            return "ok", session

    def get(self, sid: str | None) -> Session | None:
        status, session = self.lookup(sid)
        if status != "ok":
            return None
        return session

    def touch(self, session: Session) -> None:
        with self._lock:
            current = self._sessions.get(session.sid)
            if current is not None:
                current.last_seen = time.time()

    def delete(self, sid: str) -> Session | None:
        with self._lock:
            return self._sessions.pop(sid, None)

    def authenticated_sessions(self) -> list[Session]:
        with self._lock:
            return [session for session in self._sessions.values() if session.authenticated]

    def rotate(self, session: Session) -> Session:
        """Issue a new cookie id and CSRF token after login."""
        with self._lock:
            self._sessions.pop(session.sid, None)
            session.sid = secrets.token_urlsafe(32)
            session.csrf_token = secrets.token_urlsafe(32)
            session.last_seen = time.time()
            self._sessions[session.sid] = session
            return session

    def _expired(self, session: Session) -> bool:
        return (time.time() - session.last_seen) > self.idle_timeout_seconds

    def _sweep_locked(self) -> None:
        stale = [sid for sid, session in self._sessions.items() if self._expired(session)]
        for sid in stale:
            self._sessions.pop(sid, None)
