# SPDX-License-Identifier: AGPL-3.0-or-later
"""macOS process setup. The window still serves the shared FastAPI app.

No config file is required. A Jamf configuration profile can set the hidden
lab switch by writing a boolean `insecure_test_mode` in the preference domain
`org.ipad-tech-portal`. Technicians have no control for that in the window.
"""

from __future__ import annotations

import os
import plistlib
from dataclasses import replace
from pathlib import Path

import yaml

from app.config import Config, ConfigError, load_config

APP_NAME = "ipad-tech-portal"
PREFS_DOMAIN = "org.ipad-tech-portal"
PREFS_KEY = "insecure_test_mode"
LOG_MAX_BYTES = 10_485_760
LOG_RETENTION_DAYS = 90


def machine_config_path() -> Path:
    return Path("/Library/Application Support") / APP_NAME / "config.yaml"


def log_dir() -> Path:
    override = os.environ.get("ITP_MAC_LOG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Logs" / APP_NAME


def preference_files() -> list[Path]:
    """Managed preferences first, then local ones. The first file that has the key wins."""
    home = Path.home()
    library = Path("/Library")
    return [
        library / "Managed Preferences" / f"{PREFS_DOMAIN}.plist",
        home / "Library" / "Managed Preferences" / f"{PREFS_DOMAIN}.plist",
        library / "Preferences" / f"{PREFS_DOMAIN}.plist",
        home / "Library" / "Preferences" / f"{PREFS_DOMAIN}.plist",
    ]


def read_insecure_test_mode_pref() -> bool | None:
    """Return the managed or local boolean, or None when the key is absent.

    ITP_MAC_PREFS, when set, is the only file consulted. An empty value
    disables the lookup so tests do not read this Mac's real preferences.
    """
    if "ITP_MAC_PREFS" in os.environ:
        raw = os.environ["ITP_MAC_PREFS"].strip()
        if not raw:
            return None
        return _pref_from_file(Path(raw).expanduser())
    for path in preference_files():
        if not path.is_file():
            continue
        value = _pref_from_file(path)
        if value is not None:
            return value
    return None


def load_mac_config() -> Config:
    """Read an optional admin file and force the per-session Mac constraints.

    The window talks to 127.0.0.1 over HTTP. That traffic never leaves the
    Mac, so the page HTTPS check allows only a loopback Host. The socket is
    bound to 127.0.0.1 on a free port. server.host, server.port,
    server.cookie_secure, and logging.path in a file are ignored.

    The Jamf URL typed at sign-in still has to be https unless insecure test
    mode is on. A managed preference overrides the file for that switch.
    ITP_INSECURE_TEST_MODE still wins over both.
    """
    explicit = os.environ.get("ITP_CONFIG", "").strip()
    if explicit:
        config = load_config(Path(explicit).expanduser())
    else:
        machine = machine_config_path()
        if machine.is_file():
            config = load_config(machine)
        else:
            # Pass the machine path so a config.yaml in the working directory
            # is not picked up. optional=True allows the app to start with no file.
            config = load_config(machine, optional=True)

    logs = log_dir()
    audit_path = config.audit_path if _audit_path_was_set(config) else logs / "audit.jsonl"
    config = replace(
        config,
        host="127.0.0.1",
        cookie_secure=False,
        loopback_exempt=True,
        log_path=logs / "app.log",
        log_max_bytes=LOG_MAX_BYTES,
        log_retention_days=LOG_RETENTION_DAYS,
        audit_path=audit_path,
        audit_fallback_path=logs / "audit.jsonl",
    )
    if "ITP_INSECURE_TEST_MODE" not in os.environ:
        pref = read_insecure_test_mode_pref()
        if pref is not None:
            config = replace(config, insecure_test_mode=pref)
    return config


def _audit_path_was_set(config: Config) -> bool:
    if os.environ.get("ITP_AUDIT_PATH", "").strip():
        return True
    path = config.config_path
    if not path.is_file():
        return False
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return False
    audit = data.get("audit") or {}
    return isinstance(audit, dict) and bool(str(audit.get("path") or "").strip())


def _pref_from_file(path: Path) -> bool | None:
    if not path.is_file():
        raise ConfigError(f"Managed preference file not found: {path}.")
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise ConfigError(f"Could not read managed preferences {path}: {exc}") from exc
    if not isinstance(data, dict) or PREFS_KEY not in data:
        return None
    value = data[PREFS_KEY]
    if not isinstance(value, bool):
        raise ConfigError(
            "insecure_test_mode in managed preferences must be a boolean true or false."
        )
    return value
