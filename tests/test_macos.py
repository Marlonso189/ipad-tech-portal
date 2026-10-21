# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Mac app is loopback-only and reads its lab switch from a preference file."""

import logging
import plistlib
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import ConfigError
from app.jamf_client import JamfClient
from app.log_rotation import RotatingSizeAgeHandler
from app.main import create_app
from desktop.runtime import reserve_loopback_socket, start_server, stop_server, wait_until_listening
from macos.runtime import load_mac_config, read_insecure_test_mode_pref
from mock_jamf.server import create_mock_app
from tests.conftest import SyncASGITransport, make_config


def _jamf(tmp_path):
    mock = create_mock_app()
    http = httpx.Client(transport=SyncASGITransport(mock), base_url="http://jamf.test")
    jamf = JamfClient("http://jamf.test", verify_tls=False, timeout=5, client=http)
    return mock, http, jamf


def _write_plist(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump({"insecure_test_mode": value}, handle)


def _use_mac_paths(monkeypatch, tmp_path: Path) -> Path:
    machine = tmp_path / "Library" / "Application Support" / "ipad-tech-portal" / "config.yaml"
    monkeypatch.setattr("macos.runtime.machine_config_path", lambda: machine)
    monkeypatch.setenv("ITP_MAC_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("ITP_CONFIG", raising=False)
    monkeypatch.delenv("ITP_AUDIT_PATH", raising=False)
    monkeypatch.delenv("ITP_INSECURE_TEST_MODE", raising=False)
    return machine


def test_loopback_http_is_allowed_for_localhost_and_127(tmp_path):
    mock, http, jamf = _jamf(tmp_path)
    try:
        app = create_app(make_config(tmp_path, loopback_exempt=True), jamf)
        for base in ("http://127.0.0.1:8765", "http://localhost:8765"):
            with TestClient(app, base_url=base) as client:
                boot = client.get("/api/bootstrap")
                assert boot.json()["pageHttpsRequired"] is False
                blocked = client.post(
                    "/api/login",
                    json={"jamfUrl": "http://jamf.example.org", "username": "tech", "password": "demo-tech"},
                    headers={"X-CSRF-Token": boot.json()["csrfToken"]},
                )
                assert blocked.status_code == 400
                assert "https://" in blocked.json()["message"]
                allowed = client.post(
                    "/api/login",
                    json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "demo-tech"},
                    headers={"X-CSRF-Token": boot.json()["csrfToken"]},
                )
                assert allowed.status_code == 200, allowed.text
    finally:
        http.close()


def test_non_loopback_http_is_refused_even_when_forwarded_proto_says_https(tmp_path):
    mock, http, jamf = _jamf(tmp_path)
    try:
        app = create_app(make_config(tmp_path, loopback_exempt=True), jamf)
        with TestClient(app, base_url="http://portal.example") as client:
            boot = client.get("/api/bootstrap", headers={"X-Forwarded-Proto": "https"})
            assert boot.json()["pageHttpsRequired"] is True
            blocked = client.post(
                "/api/login",
                json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "demo-tech"},
                headers={"X-CSRF-Token": boot.json()["csrfToken"], "X-Forwarded-Proto": "https"},
            )
            assert blocked.status_code == 403
            assert blocked.json()["error"] == "https_required"
            assert ("POST", "/api/v1/auth/token") not in mock.state.store.requests
        with TestClient(app, base_url="https://portal.example") as client:
            boot = client.get("/api/bootstrap")
            assert boot.json()["pageHttpsRequired"] is True
            allowed = client.post(
                "/api/login",
                json={"jamfUrl": "https://jamf.example.org", "username": "tech", "password": "demo-tech"},
                headers={"X-CSRF-Token": boot.json()["csrfToken"]},
            )
            assert allowed.status_code == 200, allowed.text
    finally:
        http.close()


def test_socket_binds_loopback_and_rejects_other_loopback_addresses(tmp_path):
    sock = reserve_loopback_socket()
    host, port = sock.getsockname()[:2]
    assert host == "127.0.0.1"
    assert host != "0.0.0.0"
    app = create_app(make_config(tmp_path, loopback_exempt=True))
    server, thread = start_server(app, sock)
    try:
        wait_until_listening(server, thread, port)
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.2", port), timeout=1)
    finally:
        stop_server(server, thread)


def test_mac_config_forces_loopback_and_ignores_a_public_bind(tmp_path, monkeypatch):
    machine = _use_mac_paths(monkeypatch, tmp_path)
    machine.parent.mkdir(parents=True)
    machine.write_text(
        yaml.safe_dump(
            {
                "insecure_test_mode": False,
                "server": {"host": "0.0.0.0", "port": 8741, "cookie_secure": True},
                "logging": {"path": "logs/ignored.log", "level": "INFO"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ITP_HOST", "0.0.0.0")
    monkeypatch.setenv("ITP_COOKIE_SECURE", "true")
    config = load_mac_config()
    assert config.host == "127.0.0.1"
    assert config.cookie_secure is False
    assert config.loopback_exempt is True
    assert config.log_path == tmp_path / "logs" / "app.log"
    assert config.audit_path == tmp_path / "logs" / "audit.jsonl"
    assert config.log_max_bytes == 10_485_760
    assert config.log_retention_days == 90
    assert config.insecure_test_mode is False
    sock = reserve_loopback_socket()
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_mac_app_starts_without_a_config_file_and_ignores_the_working_directory(tmp_path, monkeypatch):
    machine = _use_mac_paths(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("insecure_test_mode: true\n", encoding="utf-8")
    config = load_mac_config()
    assert not machine.exists()
    assert config.insecure_test_mode is False
    assert config.host == "127.0.0.1"
    assert config.log_path == tmp_path / "logs" / "app.log"


def test_explicit_config_path_is_required_when_it_is_missing(tmp_path, monkeypatch):
    _use_mac_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("ITP_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(ConfigError):
        load_mac_config()


def test_explicit_audit_path_is_kept_and_logging_path_is_not(tmp_path, monkeypatch):
    machine = _use_mac_paths(monkeypatch, tmp_path)
    machine.parent.mkdir(parents=True)
    custom = tmp_path / "share" / "audit.jsonl"
    machine.write_text(
        yaml.safe_dump({"audit": {"path": str(custom)}, "logging": {"path": "logs/nope.log"}}),
        encoding="utf-8",
    )
    config = load_mac_config()
    assert config.audit_path == custom
    assert config.log_path == tmp_path / "logs" / "app.log"


def test_managed_preference_turns_on_the_banner_and_allows_http_jamf(tmp_path, monkeypatch):
    _use_mac_paths(monkeypatch, tmp_path)
    pref = tmp_path / "org.ipad-tech-portal.plist"
    _write_plist(pref, True)
    monkeypatch.setenv("ITP_MAC_PREFS", str(pref))
    config = load_mac_config()
    assert config.insecure_test_mode is True
    app = create_app(config)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        page = client.get("/")
        assert ">Insecure test mode</p>" in page.text
        assert 'id="insecure-banner" class="insecure-banner" hidden' not in page.text
        boot = client.get("/api/bootstrap")
        assert boot.json()["insecureTestMode"] is True
        assert boot.json()["pageHttpsRequired"] is False


def test_missing_or_false_preference_leaves_test_mode_off(tmp_path, monkeypatch):
    _use_mac_paths(monkeypatch, tmp_path)
    absent = tmp_path / "empty.plist"
    absent.write_bytes(plistlib.dumps({}))
    monkeypatch.setenv("ITP_MAC_PREFS", str(absent))
    assert load_mac_config().insecure_test_mode is False
    _write_plist(absent, False)
    assert read_insecure_test_mode_pref() is False
    assert load_mac_config().insecure_test_mode is False


def test_non_boolean_preference_is_rejected(tmp_path, monkeypatch):
    _use_mac_paths(monkeypatch, tmp_path)
    pref = tmp_path / "bad.plist"
    _write_plist(pref, "true")
    monkeypatch.setenv("ITP_MAC_PREFS", str(pref))
    with pytest.raises(ConfigError):
        load_mac_config()


def test_managed_preference_wins_over_a_later_file(tmp_path, monkeypatch):
    managed = tmp_path / "managed.plist"
    user = tmp_path / "user.plist"
    _write_plist(managed, True)
    _write_plist(user, False)
    monkeypatch.delenv("ITP_MAC_PREFS", raising=False)
    monkeypatch.setattr("macos.runtime.preference_files", lambda: [managed, user])
    assert read_insecure_test_mode_pref() is True


def test_environment_variable_wins_over_the_preference(tmp_path, monkeypatch):
    _use_mac_paths(monkeypatch, tmp_path)
    pref = tmp_path / "on.plist"
    _write_plist(pref, True)
    monkeypatch.setenv("ITP_MAC_PREFS", str(pref))
    monkeypatch.setenv("ITP_INSECURE_TEST_MODE", "false")
    assert load_mac_config().insecure_test_mode is False


def test_the_page_has_no_way_to_set_the_preference(tmp_path):
    app = create_app(make_config(tmp_path, loopback_exempt=True))
    paths = {getattr(route, "path", "") for route in app.routes}
    assert not any("insecure" in path or "preference" in path for path in paths)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/insecure-test-mode").status_code == 404
    page = Path("app/static/index.html").read_text(encoding="utf-8")
    script = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "insecure_test_mode" not in page
    assert "ITP_MAC_PREFS" not in script
    assert "org.ipad-tech-portal" not in script


def test_mac_log_rotates_on_size_and_age_without_a_thread(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("already here\n", encoding="utf-8")
    old = datetime.now(timezone.utc) - timedelta(days=2)
    import os

    stale = path.with_name("app-20000101T000000000000Z.log")
    stale.write_text("old\n", encoding="utf-8")
    ancient = datetime.now(timezone.utc) - timedelta(days=120)
    os.utime(stale, (ancient.timestamp(), ancient.timestamp()))
    recent = path.with_name("app-20990101T000000000000Z.log")
    recent.write_text("keep\n", encoding="utf-8")
    before = {thread.ident for thread in threading.enumerate()}
    handler = RotatingSizeAgeHandler(path, max_bytes=10_485_760, retention_days=90)
    # Opening the log can refresh its timestamp. Age is judged at the next write.
    os.utime(path, (old.timestamp(), old.timestamp()))
    try:
        record = logging.LogRecord("ipad", logging.INFO, __file__, 1, "today", (), None)
        handler.emit(record)
        rotated = list(tmp_path.glob("app-*.log"))
        assert stale not in rotated
        assert recent in rotated
        assert any(item != recent and item.read_text(encoding="utf-8").startswith("already") for item in rotated)
        assert "today" in path.read_text(encoding="utf-8")
        assert {thread.ident for thread in threading.enumerate()} == before
    finally:
        handler.close()

    tiny = tmp_path / "size.log"
    size_handler = RotatingSizeAgeHandler(tiny, max_bytes=20, retention_days=90)
    try:
        for mark in ("one", "two"):
            size_handler.emit(logging.LogRecord("ipad", logging.INFO, __file__, 1, "x" * 30 + mark, (), None))
        pieces = list(tmp_path.glob("size-*.log"))
        assert pieces
        assert pieces[0].stat().st_size > 0
        assert "two" in tiny.read_text(encoding="utf-8")
    finally:
        size_handler.close()


def test_zero_rotation_keeps_a_plain_file_handler(tmp_path):
    config = make_config(tmp_path, log_max_bytes=0, log_retention_days=0)
    create_app(config)
    logger = logging.getLogger("ipad")
    try:
        assert any(isinstance(handler, logging.FileHandler) for handler in logger.handlers)
        assert not any(isinstance(handler, RotatingSizeAgeHandler) for handler in logger.handlers)
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def test_macos_packaging_stays_unsigned_without_secrets():
    script = Path("macos/build.sh").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/macos.yml").read_text(encoding="utf-8")
    spec = Path("macos/ipad-tech-portal.spec").read_text(encoding="utf-8")
    entitlements = Path("macos/entitlements.plist").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    example = Path("macos/org.ipad-tech-portal.plist.example").read_text(encoding="utf-8")
    secrets = [
        "MACOS_CERTIFICATE_APPLICATION_P12",
        "MACOS_CERTIFICATE_APPLICATION_PASSWORD",
        "MACOS_CERTIFICATE_INSTALLER_P12",
        "MACOS_CERTIFICATE_INSTALLER_PASSWORD",
        "MACOS_NOTARY_KEY",
        "MACOS_NOTARY_KEY_ID",
        "MACOS_NOTARY_ISSUER_ID",
    ]
    for name in secrets:
        assert name in script
        assert name in workflow
        assert name in readme
    assert "MACOS_CERTIFICATE_APPLICATION_P12 is not set" in script
    assert "Building an unsigned app, pkg, and dmg." in script
    assert "Skipping notarization." in script
    assert "runs-on: macos-26" in workflow
    assert "if-no-files-found: error" in workflow
    assert "dist/ipad-tech-portal-mac.pkg" in workflow
    assert "dist/ipad-tech-portal-mac.dmg" in workflow
    assert 'LSMinimumSystemVersion": "26.0"' in spec
    assert "iPad Tech Portal.app" in spec
    assert "universal2" not in spec
    assert "com.apple.security.network.client" in entitlements
    assert "com.apple.security.network.server" not in entitlements
    assert "app-sandbox" not in entitlements
    assert "<key>insecure_test_mode</key>" in example
    assert "<false/>" in example
    for path in (
        Path("macos/__init__.py"),
        Path("macos/__main__.py"),
        Path("macos/runtime.py"),
        Path("app/log_rotation.py"),
        Path("macos/build.sh"),
        Path("macos/ipad-tech-portal.spec"),
    ):
        assert path.read_text(encoding="utf-8").startswith("# SPDX-License-Identifier: AGPL-3.0-or-later") or path.read_text(
            encoding="utf-8"
        ).splitlines()[1].startswith("# SPDX-License-Identifier: AGPL-3.0-or-later")
