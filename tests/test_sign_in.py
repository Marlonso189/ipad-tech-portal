"""Sign-in asks for the Jamf server every time and keeps it in memory only."""

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.jamf_client import JamfClient
from app.main import HTTPS_REQUIRED, create_app
from mock_jamf.server import create_mock_app
from tests.conftest import SyncASGITransport, auth_header, login, make_config


def _app(tmp_path, **overrides):
    mock = create_mock_app()
    http = httpx.Client(transport=SyncASGITransport(mock), base_url="http://jamf.test")
    jamf = JamfClient("http://jamf.test", verify_tls=False, timeout=5, client=http)
    config = make_config(tmp_path, **overrides)
    app = create_app(config, jamf)
    return app, http, mock, config


def test_jamf_url_is_required_and_http_is_rejected(portal):
    client = portal["client"]
    token = auth_header(client)
    missing = client.post(
        "/api/login",
        json={"username": "tech", "password": "demo-tech"},
        headers={**token, "X-Forwarded-Proto": "https"},
    )
    assert missing.status_code == 400
    blank = client.post(
        "/api/login",
        json={"jamfUrl": "   ", "username": "tech", "password": "demo-tech"},
        headers={**token, "X-Forwarded-Proto": "https"},
    )
    assert blank.status_code == 400
    assert blank.json()["message"] == "Enter the Jamf Pro server URL."
    rejected = client.post(
        "/api/login",
        json={"jamfUrl": "http://jamf.example.org/jamf", "username": "tech", "password": "demo-tech"},
        headers={**token, "X-Forwarded-Proto": "https"},
    )
    assert rejected.status_code == 400
    assert "https://" in rejected.json()["message"]
    assert ("POST", "/api/v1/auth/token") not in portal["mock"].state.store.requests
    assert portal["app"].state.sessions.authenticated_sessions() == []


def test_query_string_and_header_do_not_prefill_the_server(portal):
    client = portal["client"]
    page = client.get("/?jamfUrl=https://prefill.example&url=https://prefill.example&server=https://prefill.example")
    assert page.status_code == 200
    assert "prefill.example" not in page.text
    assert 'id="jamf-url"' in page.text
    assert 'id="jamf-host"' in page.text
    assert 'placeholder="https://jamf.example.org:8443"' in page.text
    script = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "URLSearchParams" not in script
    assert "location.search" not in script
    assert 'class="insecure-banner" hidden' in page.text

    token = auth_header(client)
    response = client.post(
        "/api/login?jamfUrl=https://evil.example&url=https://evil.example",
        json={"jamfUrl": "https://Jamf.Example.ORG:8443/jamf/ui/", "username": "tech", "password": "demo-tech"},
        headers={**token, "X-Forwarded-Proto": "https", "X-Jamf-Url": "https://header.example"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["jamfHost"] == "jamf.example.org:8443"
    assert "evil.example" not in response.text
    assert "header.example" not in response.text
    session = next(item for item in portal["app"].state.sessions._sessions.values() if item.authenticated)
    assert session.jamf_url == "https://jamf.example.org:8443"
    assert session.jamf_host == "jamf.example.org:8443"
    assert "jamf.example.org" not in response.headers["set-cookie"]


def test_preview_shows_the_host_and_stores_nothing(portal):
    client = portal["client"]
    token = auth_header(client)
    preview = client.post(
        "/api/jamf-url/preview",
        json={"jamfUrl": "https://jamf.example.org:8443/ignored/"},
        headers={**token, "X-Forwarded-Proto": "https"},
    )
    assert preview.status_code == 200
    assert preview.json() == {"host": "jamf.example.org:8443"}
    assert portal["mock"].state.store.requests == []
    for session in portal["app"].state.sessions._sessions.values():
        assert session.jamf_url is None
        assert session.token is None
    http_preview = client.post(
        "/api/jamf-url/preview",
        json={"jamfUrl": "http://jamf.example.org"},
        headers={**token, "X-Forwarded-Proto": "https"},
    )
    assert http_preview.status_code == 400
    assert "https://" in http_preview.json()["message"]


def test_page_over_http_is_refused_and_forwarded_https_is_accepted(portal):
    client = portal["client"]
    boot = client.get("/api/bootstrap")
    assert boot.json()["pageIsHttps"] is False
    assert boot.json()["pageHttpsRequired"] is True
    assert boot.json()["httpsRequiredMessage"] == HTTPS_REQUIRED
    assert boot.json()["jamfHost"] == ""
    assert boot.json()["insecureTestMode"] is False
    blocked = client.post(
        "/api/login",
        json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "demo-tech"},
        headers={"X-CSRF-Token": boot.json()["csrfToken"]},
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"] == "https_required"
    assert blocked.json()["message"] == HTTPS_REQUIRED
    assert ("POST", "/api/v1/auth/token") not in portal["mock"].state.store.requests

    forwarded = client.get("/api/bootstrap", headers={"X-Forwarded-Proto": "HTTPS, http"})
    assert forwarded.json()["pageIsHttps"] is True
    assert forwarded.json()["httpsRequiredMessage"] == ""
    body = login(client, headers={"X-Forwarded-Proto": "https"})
    assert body["authenticated"] is True
    assert body["jamfHost"] == "jamf.example.org:8443"


def test_https_scheme_is_accepted_without_a_forwarded_header(tmp_path):
    app, http, _mock, _config = _app(tmp_path)
    try:
        with TestClient(app, base_url="https://portal.example") as client:
            boot = client.get("/api/bootstrap")
            assert boot.json()["pageIsHttps"] is True
            response = client.post(
                "/api/login",
                json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "demo-tech"},
                headers={"X-CSRF-Token": boot.json()["csrfToken"]},
            )
            assert response.status_code == 200, response.text
    finally:
        http.close()


def test_loopback_without_the_desktop_token_is_not_exempt(tmp_path):
    app, http, _mock, _config = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            boot = client.get("/api/bootstrap")
            assert boot.json()["pageHttpsRequired"] is True
            blocked = client.post(
                "/api/login",
                json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "demo-tech"},
                headers={"X-CSRF-Token": boot.json()["csrfToken"]},
            )
            assert blocked.status_code == 403
            assert blocked.json()["error"] == "https_required"
    finally:
        http.close()


def test_insecure_test_mode_banner_allows_http_page_and_http_jamf(tmp_path):
    app, http, mock, _config = _app(tmp_path, insecure_test_mode=True)
    try:
        with TestClient(app) as client:
            page = client.get("/")
            assert ">Insecure test mode</p>" in page.text
            assert 'id="insecure-banner" class="insecure-banner" hidden' not in page.text
            banner = page.text.split('id="insecure-banner"', 1)[1].split("</p>", 1)[0]
            assert "<button" not in banner
            assert "<input" not in banner
            boot = client.get("/api/bootstrap")
            assert boot.json()["insecureTestMode"] is True
            assert boot.json()["pageHttpsRequired"] is False
            assert boot.json()["httpsRequiredMessage"] == ""
            preview = client.post(
                "/api/jamf-url/preview",
                json={"jamfUrl": "http://jamf.test:8742/jamf/"},
                headers={"X-CSRF-Token": boot.json()["csrfToken"]},
            )
            assert preview.status_code == 200
            assert preview.json()["host"] == "jamf.test:8742"
            response = client.post(
                "/api/login",
                json={"jamfUrl": "http://jamf.test:8742/jamf/", "username": "tech", "password": "demo-tech"},
                headers={"X-CSRF-Token": boot.json()["csrfToken"]},
            )
            assert response.status_code == 200, response.text
            assert response.json()["jamfHost"] == "jamf.test:8742"
            assert response.json()["insecureTestMode"] is True
            session = next(item for item in app.state.sessions._sessions.values() if item.authenticated)
            assert session.jamf_url == "http://jamf.test:8742"
            assert ("POST", "/api/v1/auth/token") in mock.state.store.requests
    finally:
        http.close()


def test_desktop_loopback_skips_page_https_but_still_requires_https_jamf(tmp_path):
    app, http, mock, _config = _app(tmp_path)
    try:
        desktop = create_app(make_config(tmp_path / "desktop"), JamfClient("http://jamf.test", False, 5, client=http), launch_token="f" * 43)
        with TestClient(desktop, base_url="http://127.0.0.1") as client:
            client.cookies.set("itp_launch", "f" * 43)
            boot = client.get("/api/bootstrap")
            assert boot.json()["pageHttpsRequired"] is False
            assert boot.json()["pageIsHttps"] is False
            assert boot.json()["httpsRequiredMessage"] == ""
            blocked = client.post(
                "/api/login",
                json={"jamfUrl": "http://jamf.example.org", "username": "tech", "password": "demo-tech"},
                headers={"X-CSRF-Token": boot.json()["csrfToken"]},
            )
            assert blocked.status_code == 400
            assert "https://" in blocked.json()["message"]
            assert ("POST", "/api/v1/auth/token") not in mock.state.store.requests
            allowed = client.post(
                "/api/login",
                json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "demo-tech"},
                headers={"X-CSRF-Token": boot.json()["csrfToken"]},
            )
            assert allowed.status_code == 200, allowed.text
    finally:
        http.close()


def test_logout_and_idle_timeout_clear_the_jamf_address(portal):
    client = portal["client"]
    login(client)
    session = next(item for item in portal["app"].state.sessions._sessions.values() if item.authenticated)
    assert session.jamf_url == "https://jamf.example.org:8443"
    assert session.token
    logged_out = client.post("/api/logout", headers=auth_header(client))
    assert logged_out.status_code == 200
    assert session.token is None
    assert session.jamf_url is None
    assert session.jamf_host is None
    assert session.username is None
    assert portal["app"].state.sessions.authenticated_sessions() == []
    again = client.get("/api/bootstrap")
    assert again.json()["jamfHost"] == ""
    assert again.json()["authenticated"] is False

    login(client)
    held = next(item for item in portal["app"].state.sessions._sessions.values() if item.authenticated)
    held.last_seen = 0
    timed = client.get("/api/devices", params={"q": "DMPX2TEST001"})
    assert timed.status_code == 401
    assert held.token is None
    assert held.jamf_url is None
    assert held.jamf_host is None


def test_only_logs_are_written_and_the_audit_line_names_the_jamf_host(portal, tmp_path):
    client = portal["client"]
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()}
    login(client, password="demo-tech")
    restarted = client.post(
        "/api/devices/101/commands/restart",
        headers=auth_header(client),
    )
    assert restarted.status_code == 200, restarted.text
    client.post("/api/logout", headers=auth_header(client))

    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()}
    for rel in after:
        assert rel.parts[0] == "logs", rel
        assert rel.name in {"app.log", "audit.jsonl", "audit.jsonl.lock"} or rel.name.startswith("audit-")
    assert after - before <= {Path("logs/audit.jsonl"), Path("logs/audit.jsonl.lock")}

    blob = ""
    for rel in after:
        if rel.suffix in {".log", ".jsonl"} or "audit" in rel.name:
            blob += (tmp_path / rel).read_text(encoding="utf-8")
    assert "demo-tech" not in blob
    assert "s3cret" not in blob
    lines = [
        json.loads(line)
        for line in (tmp_path / "logs" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines
    assert all(line["jamfHost"] == "jamf.example.org:8443" for line in lines)
    assert any(line["action"] == "restart" for line in lines)
