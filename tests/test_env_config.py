"""Environment variables override config.yaml and can replace it."""

from pathlib import Path, PurePosixPath

import pytest

from app.config import ConfigError, load_config

_ENV_KEYS = (
    "ITP_CONFIG",
    "ITP_JAMF_URL",
    "ITP_INSECURE_TEST_MODE",
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


def _clear(monkeypatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _posix_path(path) -> PurePosixPath:
    """Compare container paths without a Windows drive letter."""
    text = path.as_posix()
    if len(text) >= 2 and text[1] == ":":
        text = text[2:]
    return PurePosixPath(text)


def test_env_only_config(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITP_JAMF_URL", "https://jamf.example.org:8443/")
    monkeypatch.setenv("ITP_INSECURE_TEST_MODE", "true")
    monkeypatch.setenv("ITP_HOST", "0.0.0.0")
    monkeypatch.setenv("ITP_PORT", "8741")
    monkeypatch.setenv("ITP_GROUP_ALLOWLIST", "Tech-Loaner, Tech-Repair, ,Finance-Locked")
    monkeypatch.setenv("ITP_IDLE_TIMEOUT_MINUTES", "20")
    monkeypatch.setenv("ITP_AUDIT_RETENTION_DAYS", "30")
    monkeypatch.setenv("ITP_AUDIT_MAX_BYTES", "4096")
    monkeypatch.setenv("ITP_UPDATE_INVENTORY", "false")
    monkeypatch.setenv("ITP_VERIFY_TLS", "no")
    monkeypatch.setenv("ITP_COOKIE_SECURE", "true")
    monkeypatch.setenv("ITP_LOG_LEVEL", "warning")
    monkeypatch.setenv("ITP_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("ITP_AUDIT_PATH", "/app/logs/audit.jsonl")
    monkeypatch.setenv("ITP_LOG_PATH", "/app/logs/app.log")
    config = load_config()
    assert config.ignored_jamf_url is True
    assert not hasattr(config, "jamf_url")
    assert config.insecure_test_mode is True
    assert config.verify_tls is False
    assert config.timeout_seconds == 12
    assert config.host == "0.0.0.0"
    assert config.port == 8741
    assert config.cookie_secure is True
    assert config.idle_timeout_seconds == 20 * 60
    assert config.group_allowlist == ("Tech-Loaner", "Tech-Repair", "Finance-Locked")
    assert config.group_name_prefix == "Tech-"
    assert config.audit_retention_days == 30
    assert config.audit_max_bytes == 4096
    assert config.update_inventory_after_change is False
    assert config.log_level == "WARNING"
    assert _posix_path(config.audit_path) == PurePosixPath("/app/logs/audit.jsonl")
    assert _posix_path(config.log_path) == PurePosixPath("/app/logs/app.log")


def test_env_overrides_yaml_and_empty_strings_are_selective(tmp_path, monkeypatch):
    _clear(monkeypatch)
    destination = tmp_path / "config.yaml"
    destination.write_text(Path("config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("ITP_JAMF_URL", "")
    monkeypatch.setenv("ITP_VERIFY_TLS", "")
    monkeypatch.setenv("ITP_GROUP_ALLOWLIST", "")
    monkeypatch.setenv("ITP_GROUP_PREFIX", "")
    monkeypatch.setenv("ITP_IDLE_TIMEOUT_MINUTES", "5")
    config = load_config(destination)
    assert config.ignored_jamf_url is False
    assert config.insecure_test_mode is False
    assert config.verify_tls is True
    assert config.group_allowlist == ()
    assert config.group_name_prefix == ""
    assert config.idle_timeout_seconds == 5 * 60


def test_env_rejects_bad_values(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITP_INSECURE_TEST_MODE", "sometimes")
    with pytest.raises(ConfigError, match="ITP_INSECURE_TEST_MODE"):
        load_config()
    monkeypatch.setenv("ITP_INSECURE_TEST_MODE", "false")
    monkeypatch.setenv("ITP_VERIFY_TLS", "sometimes")
    with pytest.raises(ConfigError, match="ITP_VERIFY_TLS"):
        load_config()
    monkeypatch.setenv("ITP_VERIFY_TLS", "true")
    monkeypatch.setenv("ITP_LOG_LEVEL", "verbose")
    with pytest.raises(ConfigError, match="logging.level"):
        load_config()
    monkeypatch.delenv("ITP_LOG_LEVEL")
    monkeypatch.setenv("ITP_PORT", "0")
    with pytest.raises(ConfigError, match="server.port"):
        load_config()


def test_missing_file_starts_with_defaults(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = load_config()
    assert config.insecure_test_mode is False
    assert config.ignored_jamf_url is False
    assert config.verify_tls is True
    assert config.port == 8741
    assert config.idle_timeout_seconds == 15 * 60
