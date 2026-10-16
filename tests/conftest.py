"""Shared fixtures. Jamf is the in-process mock, not a network service."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.jamf_client import JamfClient
from app.main import create_app
from mock_jamf.server import create_mock_app


class SyncASGITransport(httpx.BaseTransport):
    """Drive an ASGI app from the portal's synchronous HTTP client."""

    def __init__(self, app) -> None:
        self.app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def _call() -> tuple[int, list[tuple[str, str]], bytes]:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://jamf.test") as client:
                target = request.url.path
                if request.url.query:
                    target = f"{target}?{request.url.query.decode()}"
                headers = {
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() not in {"host", "content-length"}
                }
                response = await client.request(request.method, target, headers=headers, content=request.content)
                return response.status_code, list(response.headers.multi_items()), response.content

        status, headers, content = asyncio.run(_call())
        return httpx.Response(status, headers=headers, content=content)

    def close(self) -> None:
        return None


_ITP_ENV = (
    "ITP_CONFIG",
    "ITP_JAMF_URL",
    "ITP_VERIFY_TLS",
    "ITP_TIMEOUT_SECONDS",
    "ITP_HOST",
    "ITP_PORT",
    "ITP_COOKIE_SECURE",
    "ITP_COOKIE_NAME",
    "ITP_IDLE_TIMEOUT_MINUTES",
    "ITP_GROUP_ALLOWLIST",
    "ITP_GROUP_PREFIX",
    "ITP_AUDIT_PATH",
    "ITP_AUDIT_RETENTION_DAYS",
    "ITP_AUDIT_MAX_BYTES",
    "ITP_UPDATE_INVENTORY",
    "ITP_LOG_PATH",
    "ITP_LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def _clear_itp_env(monkeypatch):
    for key in _ITP_ENV:
        monkeypatch.delenv(key, raising=False)


def make_config(tmp_path: Path, **overrides) -> Config:
    values = dict(
        config_path=tmp_path / "config.yaml",
        jamf_url="http://jamf.test",
        verify_tls=False,
        timeout_seconds=5,
        host="127.0.0.1",
        port=8741,
        cookie_secure=False,
        cookie_name="itp_session",
        idle_timeout_seconds=900,
        group_allowlist=(),
        group_name_prefix="Tech-",
        audit_path=tmp_path / "logs" / "audit.jsonl",
        audit_retention_days=90,
        audit_max_bytes=10_485_760,
        update_inventory_after_change=True,
        log_path=tmp_path / "logs" / "app.log",
        log_level="INFO",
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def portal(tmp_path: Path):
    mock = create_mock_app()
    http = httpx.Client(transport=SyncASGITransport(mock), base_url="http://jamf.test")
    jamf = JamfClient("http://jamf.test", verify_tls=False, timeout=5, client=http)
    config = make_config(tmp_path)
    app = create_app(config, jamf)
    with TestClient(app) as client:
        yield {"client": client, "mock": mock, "config": config, "app": app}
    http.close()


def login(client: TestClient, username: str = "tech", password: str = "demo-tech") -> dict:
    boot = client.get("/api/bootstrap")
    assert boot.status_code == 200, boot.text
    token = boot.json()["csrfToken"]
    response = client.post(
        "/api/login",
        json={"username": username, "password": password},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200, response.text
    return response.json()


def auth_header(client: TestClient) -> dict[str, str]:
    boot = client.get("/api/bootstrap")
    return {"X-CSRF-Token": boot.json()["csrfToken"]}
