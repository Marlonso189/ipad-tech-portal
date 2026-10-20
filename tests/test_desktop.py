# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-session port and launch token. Two desktops on one host do not share a server."""

import httpx
from fastapi.testclient import TestClient

from app.audit import AUDIT_FALLBACK_WARNING
from app.config import ConfigError, load_config
from app.jamf_client import JamfClient
from app.main import LAUNCH_COOKIE, create_app
from desktop.runtime import (
    invalidate_open_sessions,
    load_desktop_config,
    new_launch_token,
    reserve_loopback_socket,
    sign_in_url,
    start_server,
    stop_server,
    wait_until_listening,
)
from mock_jamf.server import create_mock_app
from tests.conftest import SyncASGITransport, login, make_config


def _cookie_flags(header: str) -> set[str]:
    return {part.strip().lower() for part in header.split(";")}


def test_loopback_sockets_are_distinct_and_local():
    first = reserve_loopback_socket()
    second = reserve_loopback_socket()
    try:
        assert first.getsockname()[0] == "127.0.0.1"
        assert second.getsockname()[0] == "127.0.0.1"
        assert first.getsockname()[1] != second.getsockname()[1]
        assert first.getsockname()[1] != 0
    finally:
        first.close()
        second.close()


def test_launch_token_is_per_process_and_cookie_is_httponly(tmp_path):
    token = "a" * 43
    other_token = "b" * 43
    config = make_config(tmp_path, cookie_secure=False)
    app = create_app(config, launch_token=token)
    client = TestClient(app, base_url="http://127.0.0.1", follow_redirects=False)

    assert client.get("/api/health").status_code == 404
    assert client.get("/", params={"t": other_token}).status_code == 404
    assert client.get("/static/app.js").status_code == 404

    foreign = TestClient(app, base_url="http://evil.example", follow_redirects=False)
    assert foreign.get("/", params={"t": token}).status_code == 404

    opened = client.get("/", params={"t": token})
    assert opened.status_code == 303
    assert opened.headers["location"] == "/"
    assert token not in opened.headers["location"]
    flags = _cookie_flags(opened.headers["set-cookie"])
    assert "httponly" in flags
    assert "samesite=strict" in flags
    assert "secure" not in flags

    client.cookies.set(LAUNCH_COOKIE, token)
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="audit-warning"' in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/bootstrap").status_code == 200

    intruder = TestClient(app, base_url="http://127.0.0.1")
    assert intruder.get("/api/bootstrap").status_code == 404
    intruder.cookies.set(LAUNCH_COOKIE, other_token)
    assert intruder.get("/api/health").status_code == 404

    log_text = config.log_path.read_text(encoding="utf-8") if config.log_path.exists() else ""
    assert token not in log_text

    mock = create_mock_app()
    http = httpx.Client(transport=SyncASGITransport(mock), base_url="http://jamf.test")
    jamf = JamfClient("http://jamf.test", verify_tls=False, timeout=5, client=http)
    guarded = create_app(make_config(tmp_path / "signed-in", cookie_secure=False), jamf, launch_token=token)
    try:
        with TestClient(guarded, base_url="http://127.0.0.1") as signed_in:
            signed_in.cookies.set(LAUNCH_COOKIE, token)
            boot = signed_in.get("/api/bootstrap")
            response = signed_in.post(
                "/api/login",
                json={
                    "jamfUrl": "https://jamf.example.org:8443/ignored",
                    "username": "tech",
                    "password": "demo-tech",
                },
                headers={"X-CSRF-Token": boot.json()["csrfToken"]},
            )
            assert response.status_code == 200
            assert response.json()["authenticated"] is True
    finally:
        http.close()


def test_launch_cookie_can_be_secure_and_login_still_needs_csrf(tmp_path):
    token = "c" * 43
    config = make_config(tmp_path, cookie_secure=True)
    mock = create_mock_app()
    http = httpx.Client(transport=SyncASGITransport(mock), base_url="http://jamf.test")
    jamf = JamfClient("http://jamf.test", verify_tls=False, timeout=5, client=http)
    app = create_app(config, jamf, launch_token=token)
    try:
        with TestClient(app, base_url="http://127.0.0.1", follow_redirects=False) as client:
            opened = client.get("/", params={"t": token})
            assert "secure" in _cookie_flags(opened.headers["set-cookie"])
            blocked = client.post(
                "/api/login",
                json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "demo-tech"},
            )
            assert blocked.status_code == 404
            client.cookies.set(LAUNCH_COOKIE, token)
            missing_csrf = client.post(
                "/api/login",
                json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "demo-tech"},
            )
            assert missing_csrf.status_code == 403
            assert missing_csrf.json()["error"] == "csrf"
            boot = client.get("/api/bootstrap")
            assert boot.status_code == 200
            # A Secure cookie is not stored by an HTTP test client. The flag check above
            # is the point of this case; the sign-in itself is covered with cookie_secure false.
            assert token not in config.log_path.read_text(encoding="utf-8")
    finally:
        http.close()


def test_two_loopback_servers_reject_each_others_tokens(tmp_path):
    tokens = ("d" * 43, "e" * 43)
    running = []
    try:
        for token in tokens:
            directory = tmp_path / token
            directory.mkdir()
            app = create_app(make_config(directory, cookie_secure=False), launch_token=token)
            sock = reserve_loopback_socket()
            port = sock.getsockname()[1]
            server, thread = start_server(app, sock)
            wait_until_listening(server, thread, port)
            assert server.config.access_log is False
            assert server.config.host == "127.0.0.1"
            running.append((token, port, server, thread))
        assert running[0][1] != running[1][1]
        port_a = running[0][1]
        with httpx.Client(timeout=3, follow_redirects=False) as client:
            assert client.get(f"http://127.0.0.1:{port_a}/api/health").status_code == 404
            wrong = client.get(f"http://127.0.0.1:{port_a}/", params={"t": tokens[1]})
            assert wrong.status_code == 404
            right = client.get(f"http://127.0.0.1:{port_a}/", params={"t": tokens[0]})
            assert right.status_code == 303
            assert tokens[0] not in right.headers["location"]
    finally:
        for _token, _port, server, thread in running:
            stop_server(server, thread)


def test_closing_the_window_invalidates_the_jamf_token(portal):
    login(portal["client"])
    assert portal["app"].state.sessions.authenticated_sessions()
    assert invalidate_open_sessions(portal["app"]) == 1
    assert ("POST", "/api/v1/auth/invalidate-token") in portal["mock"].state.store.requests
    assert portal["app"].state.sessions.authenticated_sessions() == []
    assert invalidate_open_sessions(portal["app"]) == 0


def test_desktop_config_forces_loopback_and_local_fallback(tmp_path, monkeypatch):
    program_data = tmp_path / "programdata"
    local_app = tmp_path / "localapp"
    config_dir = program_data / "ipad-tech-portal"
    config_dir.mkdir(parents=True)
    import yaml

    payload = {
        "jamf": {"verify_tls": True},
        "insecure_test_mode": False,
        "server": {"host": "0.0.0.0", "port": 8741, "cookie_secure": True},
        "session": {"idle_timeout_minutes": 15},
        "groups": {"allowlist": [], "name_prefix": "Tech-"},
        "audit": {
            "path": "\\\\files.example.org\\ipad-tech\\audit.jsonl",
            "retention_days": 90,
        },
        "logging": {"path": "logs/app.log", "level": "INFO"},
    }
    (config_dir / "config.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))
    monkeypatch.delenv("ITP_CONFIG", raising=False)
    monkeypatch.delenv("ITP_JAMF_URL", raising=False)
    config = load_desktop_config()
    assert config.host == "127.0.0.1"
    assert config.cookie_secure is False
    assert config.loopback_exempt is True
    assert config.insecure_test_mode is False
    assert not hasattr(config, "jamf_url")
    assert config.audit_fallback_path == local_app / "ipad-tech-portal" / "logs" / "audit.jsonl"
    assert config.log_path == local_app / "ipad-tech-portal" / "logs" / "app.log"
    assert str(config.audit_path).startswith("\\\\files.example.org\\")


def test_desktop_config_requires_the_machine_file(monkeypatch):
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.delenv("ITP_PROGRAMDATA", raising=False)
    monkeypatch.delenv("ITP_CONFIG", raising=False)
    try:
        load_desktop_config()
    except ConfigError as exc:
        assert "config.yaml" in str(exc)
    else:
        raise AssertionError("expected ConfigError")


def test_sign_in_url_uses_loopback_and_a_fresh_token():
    first = new_launch_token()
    second = new_launch_token()
    assert first != second
    url = sign_in_url(43123, first)
    assert url.startswith("http://127.0.0.1:43123/?t=")
    assert first in url


def test_audit_fallback_is_shown_on_the_sign_in_payload(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    config = make_config(
        tmp_path,
        audit_path=blocked / "audit.jsonl",
        audit_fallback_path=tmp_path / "local" / "audit.jsonl",
    )
    app = create_app(config)
    with TestClient(app) as client:
        body = client.get("/api/bootstrap").json()
    assert body["auditWarning"] == AUDIT_FALLBACK_WARNING
    assert (tmp_path / "local" / "audit.jsonl").exists()
    assert "password" not in AUDIT_FALLBACK_WARNING


def test_old_jamf_url_is_ignored_and_path_placeholders_expand(tmp_path, monkeypatch):
    destination = tmp_path / "config.yaml"
    text = """
jamf:
  url: "https://jamf.example.org:8443"
server:
  port: 8741
audit:
  path: "logs/audit-{host}-{user}.jsonl"
logging:
  path: "logs/app.log"
"""
    destination.write_text(text, encoding="utf-8")
    monkeypatch.setattr("app.config.socket.gethostname", lambda: "rds.01")
    monkeypatch.setattr("app.config.getpass.getuser", lambda: "../tech")
    monkeypatch.setenv("ITP_JAMF_URL", "http://mock:8742")
    config = load_config(destination)
    assert config.ignored_jamf_url is True
    assert config.insecure_test_mode is False
    assert not hasattr(config, "jamf_url")
    assert config.audit_path.name == "audit-rds_01-___tech.jsonl"
    assert config.audit_path.parent == (tmp_path / "logs").resolve()
