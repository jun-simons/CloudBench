from __future__ import annotations

import logging

import uvicorn

from cloudbench.gateway.app import create_app
from cloudbench.gateway.config import GatewaySettings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = GatewaySettings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
