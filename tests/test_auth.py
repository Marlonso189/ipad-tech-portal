import base64
import time

from tests.conftest import auth_header, login, make_config


def test_login_cookie_is_httponly_and_password_is_not_stored(portal, tmp_path):
    client = portal["client"]
    portal["mock"].state.store.users["tech"]["password"] = "s3cret-not-stored"
    response = client.get("/api/bootstrap")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "s3cret-not-stored" not in cookie

    token = response.json()["csrfToken"]
    logged_in = client.post(
        "/api/login",
        json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "s3cret-not-stored"},
        headers={"X-CSRF-Token": token, "X-Forwarded-Proto": "https"},
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["username"] == "tech"
    assert "wipe" in logged_in.json()["features"]
    assert logged_in.json()["features"]["wipe"] is True

    sessions = list(portal["app"].state.sessions._sessions.values())
    assert sessions
    assert all(not hasattr(session, "password") for session in sessions)
    blob = portal["config"].log_path.read_text(encoding="utf-8")
    basic = base64.b64encode(b"tech:s3cret-not-stored").decode("ascii")
    assert "s3cret-not-stored" not in blob
    assert basic not in blob
    assert "Bearer" not in blob


def test_secure_cookie_flag(tmp_path):
    import httpx
    from fastapi.testclient import TestClient

    from app.jamf_client import JamfClient
    from app.main import create_app
    from mock_jamf.server import create_mock_app
    from tests.conftest import SyncASGITransport

    mock = create_mock_app()
    http = httpx.Client(transport=SyncASGITransport(mock), base_url="http://jamf.test")
    app = create_app(
        make_config(tmp_path, cookie_secure=True),
        JamfClient("http://jamf.test", False, 5, client=http),
    )
    with TestClient(app) as client:
        response = client.get("/api/bootstrap")
    http.close()
    assert "secure" in response.headers["set-cookie"].lower()


def test_login_requires_csrf_and_rejects_bad_password(portal):
    client = portal["client"]
    client.get("/api/bootstrap")
    missing = client.post(
        "/api/login",
        json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "demo-tech"},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert missing.status_code == 403
    assert missing.json()["error"] == "csrf"

    bad = client.post(
        "/api/login",
        json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "not-the-password"},
        headers={**auth_header(client), "X-Forwarded-Proto": "https"},
    )
    assert bad.status_code == 401
    assert bad.json()["message"] == "Those Jamf credentials were not accepted."
    assert "not-the-password" not in portal["config"].log_path.read_text(encoding="utf-8")


def test_rejected_login_does_not_echo_password(portal):
    client = portal["client"]
    client.get("/api/bootstrap")
    response = client.post(
        "/api/login",
        json={"jamfUrl": "https://jamf.example.org", "username": "", "password": "echo-this-secret"},
        headers={**auth_header(client), "X-Forwarded-Proto": "https"},
    )
    assert response.status_code == 400
    assert "echo-this-secret" not in response.text


def test_logout_invalidates_the_jamf_token(portal):
    client = portal["client"]
    login(client)
    response = client.post("/api/logout", headers=auth_header(client))
    assert response.status_code == 200
    assert ("POST", "/api/v1/auth/invalidate-token") in portal["mock"].state.store.requests
    again = client.get("/api/devices", params={"q": "DMPX2TEST001"})
    assert again.status_code == 401


def test_idle_timeout(portal):
    client = portal["client"]
    login(client)
    session = next(item for item in portal["app"].state.sessions._sessions.values() if item.authenticated)
    session.last_seen = time.time() - portal["config"].idle_timeout_seconds - 5
    response = client.post(
        "/api/devices/101/commands/restart",
        headers={"X-CSRF-Token": session.csrf_token},
    )
    assert response.status_code == 401
    assert "timed out" in response.json()["message"]


def test_keepalive_replaces_the_token(portal):
    client = portal["client"]
    login(client)
    session = next(item for item in portal["app"].state.sessions._sessions.values() if item.authenticated)
    previous = session.token
    session.token_expires = time.time() + 30
    response = client.get("/api/devices", params={"q": "DMPX2TEST002"})
    assert response.status_code == 200
    assert session.token != previous
    assert ("POST", "/api/v1/auth/keep-alive") in portal["mock"].state.store.requests
    assert previous not in portal["config"].log_path.read_text(encoding="utf-8")


def test_desk_account_hides_wipe(portal):
    client = portal["client"]
    body = login(client, "desk", "demo-desk")
    assert body["features"]["wipe"] is False
    assert body["features"]["enable_lost_mode"] is False
    assert body["features"]["restart"] is True
    assert body["features"]["lookup"] is True
