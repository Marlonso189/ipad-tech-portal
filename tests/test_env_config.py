"""Environment variables override config.yaml and can replace it."""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path, PurePosixPath

import pytest

from app.config import ConfigError, load_config

_ENV_KEYS = (
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


def _clear(monkeypatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _posix_path(path) -> PurePosixPath:
    """Compare container paths without a Windows drive letter."""
    text = path.as_posix()
    if len(text) >= 2 and text[1] == ":":
        text = text[2:]
    return PurePosixPath(text)


def _caddy_script() -> str:
    text = Path("deploy/docker-compose.yml").read_text(encoding="utf-8")
    raw = text.split("# itp-caddy-entrypoint\n", 1)[1].split("# end itp-caddy-entrypoint", 1)[0]
    return textwrap.dedent(raw).replace("$$", "$")


def test_env_only_config(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITP_JAMF_URL", "https://jamf.example.org:8443/")
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
    assert config.jamf_url == "https://jamf.example.org:8443"
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
    assert config.jamf_url == "https://jamf.example.org:8443"
    assert config.verify_tls is True
    assert config.group_allowlist == ()
    assert config.group_name_prefix == ""
    assert config.idle_timeout_seconds == 5 * 60


def test_env_rejects_bad_values(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ITP_JAMF_URL", "jamf.example.org")
    with pytest.raises(ConfigError, match="ITP_JAMF_URL"):
        load_config()
    monkeypatch.setenv("ITP_JAMF_URL", "https://jamf.example.org:8443")
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


def test_missing_file_without_url_names_both_options(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="ITP_JAMF_URL"):
        load_config()


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None,
    reason="The Caddy entrypoint is a Linux container script and needs sh.",
)
def test_deploy_caddy_entrypoint_modes(tmp_path):
    script = _caddy_script()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "caddy"
    args_path = tmp_path / "args"
    fake.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$VERIFY_ARGS\"\nexit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["VERIFY_ARGS"] = str(args_path)

    env.update({"ITP_HOSTNAME": "ipad-tech.example.org", "ITP_TLS_CERT": "", "ITP_TLS_KEY": ""})
    subprocess.run(["sh", "-c", script], check=True, env=env)
    generated = Path("/tmp/Caddyfile").read_text(encoding="utf-8")
    assert "tls internal" in generated
    assert "ipad-tech.example.org {" in generated
    assert "reverse_proxy portal:8741" in generated
    assert args_path.read_text(encoding="utf-8") == "run --config /tmp/Caddyfile --adapter caddyfile\n"

    env.update({"ITP_TLS_CERT": "/certs/cert.pem", "ITP_TLS_KEY": "/certs/key.pem"})
    subprocess.run(["sh", "-c", script], check=True, env=env)
    generated = Path("/tmp/Caddyfile").read_text(encoding="utf-8")
    assert "tls /certs/cert.pem /certs/key.pem" in generated

    env.update({"ITP_HOSTNAME": "not a host", "ITP_TLS_CERT": "", "ITP_TLS_KEY": ""})
    failed = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)
    assert failed.returncode != 0
    assert "ITP_HOSTNAME" in failed.stderr

    env.update({"ITP_HOSTNAME": "ipad-tech.example.org", "ITP_TLS_CERT": "/certs/cert.pem", "ITP_TLS_KEY": ""})
    failed = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)
    assert failed.returncode != 0
    assert "ITP_TLS_KEY" in failed.stderr
    Path("/tmp/Caddyfile").unlink(missing_ok=True)
