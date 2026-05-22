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
    yaaos_env: Literal["dev", "test", "prod"] = "prod"
    yaaos_port: int = 8080
    yaaos_cors_origins: str | None = None  # comma-separated; only honored in non-dev
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "yaaos"
    log_level: str = "INFO"

    # GitHub API base URL — overridden in the test stack to point at `apps/fake-github`.
    github_api_base_url: str = "https://api.github.com"

    # M05 — Redis backs taskiq workflow tasks + sse_pubsub fanout. Required
    # at runtime once `core/tasks` workers and SSE handlers are wired; optional
    # at boot so the existing M01-M04 surface still starts without Redis.
    redis_url: str | None = None

    # core/llm gateway. Both unset = direct provider calls via ANTHROPIC_API_KEY.
    braintrust_api_key: str | None = None
    braintrust_api_url: str | None = None  # e.g. https://gateway.braintrust.dev
    # Name of the Braintrust project that gateway calls log into. Without this
    # the gateway is a pure pass-through and nothing appears in the Logs tab.
    # The project is auto-created on first request if it doesn't exist.
    braintrust_project: str = "yaaos"

    # Time controls. Production defaults are reasonable; tests set short.
    # See plan/milestones/M01-code-review/patterns.md § Time controls.
    yaaos_review_debounce_seconds: int = 30
    yaaos_reaper_interval_seconds: int = 30
    yaaos_heartbeat_interval_seconds: int = 10
    yaaos_catchup_delay_seconds: int = 10

    # M02 — session lifetime + cleanup cadence.
    yaaos_session_lifetime_seconds: int = 60 * 60 * 24 * 14  # 14 days
    yaaos_auth_cleanup_interval_seconds: int = 60 * 60  # 1 hour
    yaaos_integrations_health_check_interval_seconds: int = 60 * 60  # 1 hour

    # M02 — OAuth GitHub credentials. Required in `prod`; defaults let `dev`
    # boot without provisioning. Tests override via env at fixture time.
    yaaos_oauth_github_client_id: str = ""
    yaaos_oauth_github_client_secret: str = ""
    yaaos_oauth_github_authorize_url: str = "https://github.com/login/oauth/authorize"
    yaaos_oauth_github_token_url: str = "https://github.com/login/oauth/access_token"
    yaaos_oauth_github_userinfo_url: str = "https://api.github.com/user"
    yaaos_oauth_github_emails_url: str = "https://api.github.com/user/emails"
    yaaos_oauth_state_secret: str = "dev-only-oauth-state-secret"

    # M02 — TOTP master key (Fernet, 32 bytes URL-safe base64). Defaults to
    # empty; `domain/identity.totp` falls back to `yaaos_encryption_key` when
    # unset so dev/test only need one key. Production must set this.
    yaaos_totp_master_key: str = ""

    # M04 — Linear OAuth + hosted MCP. Defaults point at the real upstreams;
    # the test compose overrides to fake-linear hostnames.
    yaaos_oauth_linear_client_id: str = ""
    yaaos_oauth_linear_client_secret: str = ""
    linear_oauth_authorize_url: str = "https://linear.app/oauth/authorize"
    linear_oauth_token_url: str = "https://api.linear.app/oauth/token"
    linear_oauth_refresh_url: str = "https://api.linear.app/oauth/token"
    linear_mcp_url: str = "https://mcp.linear.app/sse"
    linear_api_base_url: str = "https://api.linear.app"

    # M04 — Notion OAuth + hosted MCP. Same shape; Notion uses HTTP Basic
    # on the token endpoint, encoded in the provider config rather than here.
    yaaos_oauth_notion_client_id: str = ""
    yaaos_oauth_notion_client_secret: str = ""
    notion_oauth_authorize_url: str = "https://api.notion.com/v1/oauth/authorize"
    notion_oauth_token_url: str = "https://api.notion.com/v1/oauth/token"
    notion_oauth_refresh_url: str = "https://api.notion.com/v1/oauth/token"
    notion_mcp_url: str = "https://mcp.notion.com/mcp"
    notion_api_base_url: str = "https://api.notion.com"

    # M02 — invitations + dev SMTP (Mailpit).
    yaaos_invitation_token_secret: str = "dev-only-invitation-secret"
    yaaos_invitation_lifetime_seconds: int = 60 * 60 * 24 * 7  # 7 days
    yaaos_app_base_url: str = "http://localhost:8080"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "yaaos@localhost"
    smtp_use_tls: bool = False

    @property
    def is_non_prod(self) -> bool:
        """True when `yaaos_env` is `dev` or `test`. Use for affordances that
        should be permissive in both (NullPool, no-Secure cookies, etc.)."""
        return self.yaaos_env != "prod"

    @property
    def cors_origins_list(self) -> list[str]:
        if self.is_non_prod:
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
