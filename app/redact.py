# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strip passwords, tokens, and authorization material from logs."""

from __future__ import annotations

import re
from typing import Any

_SECRET_KEYS = {
    "password",
    "passwd",
    "token",
    "authorization",
    "secret",
    "cookie",
    "set-cookie",
    "csrf",
    "csrftoken",
    "bearer",
}

_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-+/=]+")
_BASIC = re.compile(r"(?i)\b(basic\s+)[A-Za-z0-9+/=]+")


def redact(value: Any) -> Any:
    """Return a copy of value with secret-looking fields replaced."""
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _key_is_secret(key):
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    text = _BEARER.sub(r"\1[redacted]", text)
    text = _BASIC.sub(r"\1[redacted]", text)
    return text


def _key_is_secret(key: str) -> bool:
    folded = re.sub(r"[^a-z0-9]", "", key.casefold())
    return folded in _SECRET_KEYS
