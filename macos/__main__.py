# SPDX-License-Identifier: AGPL-3.0-or-later
"""Open the portal in a Mac window and invalidate Jamf tokens when it closes.

Run with `python -m macos` during development. The installed app uses the
same entry. pywebview uses WKWebView and has to run on the main thread.
uvicorn runs on a background thread.
"""

from __future__ import annotations

import subprocess
import sys

from app.config import ConfigError
from app.main import create_app
from desktop.runtime import (
    invalidate_open_sessions,
    new_launch_token,
    reserve_loopback_socket,
    sign_in_url,
    start_server,
    stop_server,
    wait_until_listening,
)
from macos.runtime import load_mac_config


def main() -> None:
    try:
        config = load_mac_config()
    except ConfigError as exc:
        _fail(str(exc))
        return

    try:
        import webview
    except ImportError:
        _fail(
            "The Mac window needs pywebview. Install requirements-desktop.txt, then start the portal again."
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
    _notify(message)
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _notify(message: str) -> None:
    if sys.platform != "darwin":
        return
    script = f"display dialog {_applescript_string(message)} with title \"iPad Tech Portal\" buttons {{\"OK\"}} default button \"OK\" with icon stop"
    try:
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    except OSError:
        return


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


if __name__ == "__main__":
    main()
