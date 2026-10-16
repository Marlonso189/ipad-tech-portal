"""Serve the local Jamf stand-in. Default port is 8742."""

from __future__ import annotations

import os

import uvicorn

from mock_jamf.server import create_mock_app


def main() -> None:
    port = int(os.environ.get("MOCK_JAMF_PORT", "8742"))
    host = os.environ.get("MOCK_JAMF_HOST", "127.0.0.1")
    uvicorn.run(create_mock_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
