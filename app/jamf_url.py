# SPDX-License-Identifier: AGPL-3.0-or-later
"""Normalize a Jamf Pro base URL. Nothing here is stored."""

from __future__ import annotations

from urllib.parse import urlsplit


class JamfUrlError(Exception):
    """The technician's Jamf URL cannot be used."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def normalize_jamf_url(raw: str, *, allow_http: bool) -> tuple[str, str]:
    """Return ``(base_url, host)`` for a Jamf server address.

    ``base_url`` is ``https://host`` or, in insecure test mode, ``http://host``,
    with no userinfo, path, query, or fragment. A default port is omitted.
    ``host`` is the hostname technicians should see before they submit a password,
    including a non-default port. The raw value is never echoed in an error.
    """
    text = raw.strip()
    if not text:
        raise JamfUrlError("Enter the Jamf Pro server URL.")
    if any(char.isspace() for char in text) or "\\" in text:
        raise JamfUrlError("The Jamf Pro server URL must use https://.")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise JamfUrlError("Enter a Jamf Pro server URL such as https://jamf.example.org.") from None

    scheme = parts.scheme.casefold()
    if scheme == "http":
        if not allow_http:
            raise JamfUrlError("The Jamf Pro server URL must use https://.")
    elif scheme != "https":
        raise JamfUrlError("The Jamf Pro server URL must use https://.")
    if parts.username is not None or parts.password is not None:
        raise JamfUrlError("The Jamf Pro server URL cannot include a username or password.")

    hostname = parts.hostname
    if not hostname:
        raise JamfUrlError("Enter a Jamf Pro server URL such as https://jamf.example.org.")
    try:
        host_ascii = hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    except UnicodeError:
        raise JamfUrlError("That Jamf Pro server name is not valid.") from None
    if not host_ascii or not _host_ok(host_ascii):
        raise JamfUrlError("That Jamf Pro server name is not valid.")
    try:
        port = parts.port
    except ValueError:
        raise JamfUrlError("That Jamf Pro server port is not valid.") from None
    if port is not None and not 1 <= port <= 65535:
        raise JamfUrlError("That Jamf Pro server port is not valid.")

    default_port = 443 if scheme == "https" else 80
    display = _format_host(host_ascii, port, default_port)
    return f"{scheme}://{display}", display


def _host_ok(host: str) -> bool:
    if host == "localhost":
        return True
    # urlsplit already accepted an IPv6 hostname. It has no brackets here.
    if ":" in host:
        return True
    labels = host.split(".")
    if not labels or any(not label or len(label) > 63 for label in labels):
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    return all(set(label) <= allowed and not label.startswith("-") and not label.endswith("-") for label in labels)


def _format_host(host: str, port: int | None, default_port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    if port is None or port == default_port:
        return shown
    return f"{shown}:{port}"
