"""Boot-time configuration via pydantic-settings.

Reads from process env, falling back to `.env` files in the multi-file precedence:
  .env.{ENV}.local  (gitignored)
  .env.{ENV}
  .env.local        (gitignored)
  .env

See `plan/milestones/M01-code-review/architecture.md` § Boot-time environment
variables for the canonical list.
"""

from functools import cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All boot-time env vars consumed by the app.

    Required fields raise at construction if unset; optional fields have defaults.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local", ".env.dev", ".env.dev.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Required
    database_url: str = Field(
        ...,
        description="Async Postgres URL (e.g., postgresql+asyncpg://user:pw@host:port/db).",
    )
    yaaos_encryption_key: str = Field(
        ...,
        description="Fernet key (32 bytes, URL-safe base64) for credential encryption at rest.",
    )

    # Optional
    yaaos_env: Literal["dev", "prod"] = "prod"
    yaaos_port: int = 8080
    yaaos_cors_origins: str | None = None  # comma-separated; only honored in non-dev
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "yaaos"
    log_level: str = "INFO"

    # GitHub API base URL — overridden in the test stack to point at `apps/fake-github`.
    github_api_base_url: str = "https://api.github.com"

    # core/llm gateway. Both unset = direct provider calls via ANTHROPIC_API_KEY.
    braintrust_api_key: str | None = None
    braintrust_api_url: str | None = None  # e.g. https://api.braintrust.dev/v1/proxy

    # Time controls. Production defaults are reasonable; tests set short.
    # See plan/milestones/M01-code-review/patterns.md § Time controls.
    yaaos_review_debounce_seconds: int = 30
    yaaos_reaper_interval_seconds: int = 30
    yaaos_heartbeat_interval_seconds: int = 10
    yaaos_catchup_delay_seconds: int = 10

    @property
    def cors_origins_list(self) -> list[str]:
        if self.yaaos_env == "dev":
            return ["*"]
        if not self.yaaos_cors_origins:
            return []
        return [o.strip() for o in self.yaaos_cors_origins.split(",") if o.strip()]

    @property
    def otel_enabled(self) -> bool:
        return bool(self.otel_exporter_otlp_endpoint)


@cache
def get_settings() -> Settings:
    """Return the singleton Settings instance. Cached so subsequent calls are free."""
    return Settings()  # type: ignore[call-arg]
