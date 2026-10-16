"""Desktop process setup. The window still serves the shared FastAPI app."""

from __future__ import annotations

import os
import secrets
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI

from app.config import Config, ConfigError, load_config

APP_NAME = "ipad-tech-portal"


def machine_config_path() -> Path:
    base = os.environ.get("PROGRAMDATA") or os.environ.get("ITP_PROGRAMDATA")
    if not base:
        raise ConfigError(
            "The machine configuration was not found. An administrator should create "
            "%ProgramData%\\ipad-tech-portal\\config.yaml from desktop/config.example.yaml."
        )
    return Path(base) / APP_NAME / "config.yaml"


def local_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("ITP_LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def load_desktop_config() -> Config:
    """Read the admin's config and force the per-session desktop constraints.

    The window talks to 127.0.0.1 over HTTP, so the Secure cookie flag is forced
    off or WebView2 will drop the session. Isolation is the launch token, not TLS.
    server.host and server.port in the file are ignored: each process binds
    127.0.0.1 on a free port. The application log is per user because ProgramData
    is not writable by technicians.
    """
    explicit = os.environ.get("ITP_CONFIG")
    path = Path(explicit).expanduser() if explicit else machine_config_path()
    config = load_config(path)
    local = local_data_dir()
    return replace(
        config,
        host="127.0.0.1",
        cookie_secure=False,
        audit_fallback_path=local / "logs" / "audit.jsonl",
        log_path=local / "logs" / "app.log",
    )


def reserve_loopback_socket() -> socket.socket:
    """Bind 127.0.0.1:0. The caller passes the socket to uvicorn, so the port cannot be taken in between."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


def new_launch_token() -> str:
    return secrets.token_urlsafe(32)


def sign_in_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/?t={quote(token, safe='')}"


def start_server(app: FastAPI, sock: socket.socket) -> tuple[uvicorn.Server, threading.Thread]:
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
        workers=1,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=lambda: server.run(sockets=[sock]),
        name="ipad-tech-portal",
        daemon=False,
    )
    thread.start()
    return server, thread


def wait_until_listening(
    server: uvicorn.Server,
    thread: threading.Thread,
    port: int,
    timeout: float = 10,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not thread.is_alive() and not server.started:
            raise RuntimeError("The local portal stopped before it accepted connections.")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("The local portal did not start listening on 127.0.0.1.")


def stop_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=8)


def invalidate_open_sessions(app: FastAPI) -> int:
    """Invalidate every Jamf token this process still holds. Safe to call twice."""
    portal = app.state.portal
    store = app.state.sessions
    sessions = store.authenticated_sessions()
    for session in sessions:
        try:
            portal.logout(session)
        finally:
            store.delete(session.sid)
    return len(sessions)
