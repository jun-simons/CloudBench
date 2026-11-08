from __future__ import annotations

import logging
import re

import uvicorn

from cloudbench.gateway.app import create_app
from cloudbench.gateway.config import GatewaySettings

_TOKEN_PARAM = re.compile(r"(access_token=)[^&\s\"]+")


class RedactTokens(logging.Filter):
    """Keep session tokens passed as ?access_token= (EventSource, <img>) out of logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_PARAM.sub(r"\1[redacted]", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("uvicorn.access").addFilter(RedactTokens())
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
