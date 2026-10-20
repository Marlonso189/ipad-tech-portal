# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the portal. Bind address and port come from config.yaml."""

from __future__ import annotations

import uvicorn

from app.config import load_config
from app.main import create_app


def main() -> None:
    config = load_config()
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level=config.log_level.lower(),
        access_log=True,
    )


if __name__ == "__main__":
    main()
