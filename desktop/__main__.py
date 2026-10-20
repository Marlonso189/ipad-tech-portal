# SPDX-License-Identifier: AGPL-3.0-or-later
"""Open the portal in a desktop window and invalidate Jamf tokens when it closes.

Run with `python -m desktop` during development. The installed executable uses
the same entry. pywebview has to run on the main thread on Windows; uvicorn
runs on a background thread. uvicorn already skips signal handlers off that thread.
"""

from __future__ import annotations

import os
import sys

from app.config import ConfigError
from app.main import create_app
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


def main() -> None:
    try:
        config = load_desktop_config()
    except ConfigError as exc:
        _fail(str(exc))
        return

    try:
        import webview
    except ImportError:
        _fail(
            "The desktop window needs pywebview and, on Windows, the Microsoft Edge WebView2 Runtime. "
            "Install requirements-desktop.txt, then start the portal again."
        )
        return

    token = new_launch_token()
    sock = reserve_loopback_socket()
    port = sock.getsockname()[1]
    app = create_app(config, launch_token=token)
    server, thread = start_server(app, sock)
    try:
        wait_until_listening(server, thread, port)
        window = webview.create_window(
            "iPad Tech Portal",
            sign_in_url(port, token),
            width=1100,
            height=800,
            min_size=(420, 640),
        )

        def _close() -> None:
            invalidate_open_sessions(app)
            stop_server(server, thread)

        window.events.closed += _close
        webview.start()
    except Exception as exc:
        _fail(f"The iPad Tech Portal window could not open. {exc}")
    finally:
        invalidate_open_sessions(app)
        stop_server(server, thread)


def _fail(message: str) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "iPad Tech Portal", 0x10)
    print(message, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
