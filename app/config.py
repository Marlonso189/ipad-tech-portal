"""Load the portal configuration. The file contains no secrets.

A YAML file is optional. Environment variables override the file, and they are
enough on their own. An empty string does not override a value, except
ITP_GROUP_ALLOWLIST and ITP_GROUP_PREFIX, where empty is meaningful.

The Jamf Pro address is not a setting. Each technician types it on the sign-in
page, and it lives only in that process's session. jamf.url and ITP_JAMF_URL
are ignored so an old file does not pin the server.
"""

from __future__ import annotations

import getpass
import os
import socket
from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(Exception):
    """The configuration file is missing or invalid."""


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


@dataclass(frozen=True)
class Config:
    config_path: Path
    verify_tls: bool
    timeout_seconds: float
    host: str
    port: int
    cookie_secure: bool
    cookie_name: str
    idle_timeout_seconds: int
    group_allowlist: tuple[str, ...]
    group_name_prefix: str
    audit_path: Path
    audit_retention_days: int
    audit_max_bytes: int
    update_inventory_after_change: bool
    log_path: Path
    log_level: str
    # Lab switch. Off unless ITP_INSECURE_TEST_MODE or insecure_test_mode says so.
    # There is no control for it in the page.
    insecure_test_mode: bool = False
    # Set by the desktop launcher. The window is HTTP on loopback, which is
    # allowed. A Jamf URL still has to be https unless insecure test mode is on.
    loopback_exempt: bool = False
    # True when an old jamf.url or ITP_JAMF_URL was present and ignored.
    ignored_jamf_url: bool = False
    # Set by the desktop launcher. The service and the container leave this empty
    # so a bad audit path fails startup instead of hiding the failure.
    audit_fallback_path: Path | None = None


def load_config(path: str | Path | None = None) -> Config:
    explicit = path is not None or "ITP_CONFIG" in os.environ
    raw_path = path if path is not None else os.environ.get("ITP_CONFIG") or "config.yaml"
    config_path = _anchor(raw_path)
    data: dict = {}
    if config_path.is_file():
        data = _read_yaml(config_path)
    elif explicit:
        raise ConfigError(
            f"Configuration file not found: {config_path}. "
            "Copy config.example.yaml to config.yaml and edit it."
        )

    jamf = _section(data, "jamf")
    server = _section(data, "server")
    session = _section(data, "session")
    groups = _section(data, "groups")
    audit = _section(data, "audit")
    inventory = _section(data, "inventory")
    logging_cfg = _section(data, "logging")

    ignored_jamf_url = not _blank(os.environ.get("ITP_JAMF_URL")) or not _blank(str(jamf.get("url") or ""))

    timeout = _env_float("ITP_TIMEOUT_SECONDS", float(jamf.get("timeout_seconds") or 30))
    if timeout <= 0:
        raise ConfigError("jamf.timeout_seconds must be greater than zero.")

    port = _env_int("ITP_PORT", int(server.get("port") or 8741))
    if not 1 <= port <= 65535:
        raise ConfigError("server.port must be between 1 and 65535.")

    idle_minutes = _env_int("ITP_IDLE_TIMEOUT_MINUTES", int(session.get("idle_timeout_minutes") or 15))
    if idle_minutes <= 0:
        raise ConfigError("session.idle_timeout_minutes must be greater than zero.")

    allowlist_env = _env_raw("ITP_GROUP_ALLOWLIST")
    if allowlist_env is None:
        allowlist_raw = groups.get("allowlist") or []
        if not isinstance(allowlist_raw, list) or not all(isinstance(item, str) for item in allowlist_raw):
            raise ConfigError("groups.allowlist must be a list of strings.")
        allowlist = tuple(item.strip() for item in allowlist_raw if item.strip())
    else:
        allowlist = tuple(item.strip() for item in allowlist_env.split(",") if item.strip())

    prefix_env = _env_raw("ITP_GROUP_PREFIX")
    if prefix_env is None:
        if "name_prefix" in groups:
            prefix = str(groups.get("name_prefix") or "")
        else:
            prefix = "Tech-"
    else:
        prefix = prefix_env.strip()

    retention_days = _env_int("ITP_AUDIT_RETENTION_DAYS", int(audit.get("retention_days") or 90))
    if retention_days <= 0:
        raise ConfigError("audit.retention_days must be greater than zero.")
    max_bytes = _env_int("ITP_AUDIT_MAX_BYTES", int(audit.get("max_bytes") or 10_485_760))
    if max_bytes <= 0:
        raise ConfigError("audit.max_bytes must be greater than zero.")

    cookie_name = _env_text("ITP_COOKIE_NAME", str(server.get("cookie_name") or "itp_session"))
    if not cookie_name:
        raise ConfigError("server.cookie_name must not be empty.")

    level = _env_text("ITP_LOG_LEVEL", str(logging_cfg.get("level") or "INFO")).upper()
    if level not in _LOG_LEVELS:
        raise ConfigError("logging.level must be DEBUG, INFO, WARNING, or ERROR.")

    audit_path = _env_text("ITP_AUDIT_PATH", str(audit.get("path") or "logs/audit.jsonl"))
    log_path = _env_text("ITP_LOG_PATH", str(logging_cfg.get("path") or "logs/app.log"))

    file_test_mode = data.get("insecure_test_mode", False)
    if not isinstance(file_test_mode, bool):
        raise ConfigError("insecure_test_mode must be true or false.")
    insecure_test_mode = _env_bool("ITP_INSECURE_TEST_MODE", file_test_mode)

    return Config(
        config_path=config_path,
        verify_tls=_env_bool("ITP_VERIFY_TLS", bool(jamf.get("verify_tls", True))),
        timeout_seconds=timeout,
        host=_env_text("ITP_HOST", str(server.get("host") or "127.0.0.1")),
        port=port,
        cookie_secure=_env_bool("ITP_COOKIE_SECURE", bool(server.get("cookie_secure", True))),
        cookie_name=cookie_name,
        idle_timeout_seconds=idle_minutes * 60,
        group_allowlist=allowlist,
        group_name_prefix=prefix,
        audit_path=_resolve_path(config_path, audit_path),
        audit_retention_days=retention_days,
        audit_max_bytes=max_bytes,
        update_inventory_after_change=_env_bool(
            "ITP_UPDATE_INVENTORY",
            bool(inventory.get("update_inventory_after_change", True)),
        ),
        log_path=_resolve_path(config_path, log_path),
        log_level=level,
        insecure_test_mode=insecure_test_mode,
        ignored_jamf_url=ignored_jamf_url,
    )


def _read_yaml(config_path: Path) -> dict:
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("The configuration file must be a YAML mapping.")
    return data


def _section(data: dict, name: str) -> dict:
    value = data.get(name) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping.")
    return value


def _anchor(raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return path.resolve(strict=False)
    except OSError:
        return path


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _env_raw(name: str) -> str | None:
    if name not in os.environ:
        return None
    return os.environ[name]


def _env_text(name: str, current: str) -> str:
    raw = _env_raw(name)
    if raw is None or not raw.strip():
        return current
    return raw.strip()


def _env_bool(name: str, current: bool) -> bool:
    raw = _env_raw(name)
    if raw is None or not raw.strip():
        return current
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false.")


def _env_int(name: str, current: int) -> int:
    raw = _env_raw(name)
    if raw is None or not raw.strip():
        return current
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer.") from exc


def _env_float(name: str, current: float) -> float:
    raw = _env_raw(name)
    if raw is None or not raw.strip():
        return current
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number.") from exc


def _resolve_path(config_path: Path, value: str) -> Path:
    expanded = _expand_path_tokens(value)
    # Do not resolve a UNC path. A share that is down can stall resolve()
    # for the Windows network timeout, and the desktop app still has to start.
    if _is_unc(expanded):
        return Path(expanded)
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    try:
        return path.resolve(strict=False)
    except OSError:
        return path


def _is_unc(value: str) -> bool:
    return value.startswith("\\\\") or value.startswith("//")


def _expand_path_tokens(value: str) -> str:
    if "{host}" not in value and "{user}" not in value:
        return value
    return value.replace("{host}", _path_token(socket.gethostname())).replace(
        "{user}", _path_token(getpass.getuser())
    )


def _path_token(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value.strip())
    return cleaned[:64] or "unknown"
