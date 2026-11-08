from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    """Gateway configuration, read from ``CLOUDBENCH_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="CLOUDBENCH_", env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000

    data_dir: Path = Path("data")
    experiments_dir: Path = Path("experiments")
    # Built web app to serve at "/". Optional; the API works without it.
    web_dir: Path | None = None

    # Secret used to sign short-lived file download URLs for agents.
    signing_secret: str = Field(default="dev-signing-secret-change-me", min_length=16)
    # Per-workbench agent secrets, e.g. {"wb_faraday": "s3cret"}.
    # Env form: CLOUDBENCH_AGENT_SECRETS='{"wb_faraday": "s3cret"}'
    agent_secrets: dict[str, str] = Field(default_factory=dict)
    # Bearer token for /admin endpoints. Admin endpoints are disabled when unset.
    admin_token: str | None = None

    # Leases
    lease_max_duration_s: float = 180
    lease_offer_timeout_s: float = 30
    # A lease holder with no API request or open event stream for this long loses the lease.
    lease_idle_timeout_s: float = 60
    # How long to wait for an agent to confirm its safe-state reset before moving on.
    reset_timeout_s: float = 10

    # Agents
    agent_heartbeat_interval_s: float = 5
    # An agent that sends nothing for this long is considered offline.
    agent_timeout_s: float = 15

    # Files
    max_upload_bytes: int = 20 * 1024 * 1024
    file_url_ttl_s: float = 120

    # Event stream keepalive comment interval.
    sse_keepalive_s: float = 15

    # Interval of the background loop that enforces lease deadlines.
    tick_interval_s: float = 0.5

    @property
    def database_path(self) -> Path:
        return self.data_dir / "cloudbench.sqlite3"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"
