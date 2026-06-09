"""Boot-time configuration via pydantic-settings.

Reads from process env, falling back to `.env` files in the multi-file precedence:
  .env.{ENV}.local  (gitignored)
  .env.{ENV}
  .env.local        (gitignored)
  .env

See `Boot-time environment
variables for the canonical list.
"""

from functools import cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, computed_field
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
    yaaos_encryption_key: SecretStr = Field(
        ...,
        description="Fernet key (32 bytes, URL-safe base64) for credential encryption at rest.",
    )
    redis_url: str = Field(
        ...,
        description="Redis URL (e.g., redis://host:port/db). Backs core/sse fanout and the core/tasks taskiq broker.",
    )

    # Optional
    yaaos_env: Literal["dev", "test", "prod"] = "prod"
    yaaos_port: int = 8080

    # SQLAlchemy QueuePool sizing for the prod-path engine. NullPool is used
    # in dev/test (see `core/database`), so these only affect prod-mode
    # processes. Pool size tracks concurrent in-flight queries, not event
    # loops — a single asyncio loop can hold many connections at once when
    # multiple coroutines `await` on queries simultaneously. Worker rule of
    # thumb: db_pool_size >= worker_concurrency + 2 (drain + headroom).
    db_pool_size: int = 10
    db_max_overflow: int = 5
    yaaos_cors_origins: str | None = None  # comma-separated; only honored in non-dev
    otel_exporter_otlp_endpoint: str | None = None
    # OTel `service.name` per role. App + worker deploy as separate processes
    # so they get distinct service identities — per OTel semconv, service.name
    # must differ across separately-deployed roles even when they share a code
    # base. Both env-overridable for downstream collectors that need a custom
    # naming scheme.
    otel_service_name_app: str = "yaaos-app"
    otel_service_name_worker: str = "yaaos-worker"
    log_level: str = "INFO"

    # GitHub API base URL — overridden in the test stack to point at `apps/fake-github`.
    github_api_base_url: str = "https://api.github.com"
    # GitHub *web* base URL — the install picker lives at
    # `<github_web_base_url>/apps/<slug>/installations/new`. Separate from the
    # API base because GitHub serves them on different origins; e2e overrides
    # this to point at fake-github so the browser-side redirect chain works
    # without leaving the docker network.
    github_web_base_url: str = "https://github.com"
    # GitHub *git* base URL — the origin the workspace agent clones from
    # (`<github_git_base_url>/<owner>/<repo>.git`). Normally identical to the web
    # base (real github.com serves both), but the test stack splits the
    # browser-facing host (`localhost:58081`, where Playwright follows OAuth
    # redirects) from the docker-internal host (`fake-github:8080`, where the
    # agent container can actually reach the git server). Unset → defaults to
    # web-base. Mirrors the `yaaos_github_oauth_token_url` host-split below.
    github_git_base_url: str = ""

    # core/llm gateway. Both unset = direct provider calls via ANTHROPIC_API_KEY.
    braintrust_api_key: SecretStr | None = None
    braintrust_api_url: str | None = None  # e.g. https://gateway.braintrust.dev
    # Name of the Braintrust project that gateway calls log into. Without this
    # the gateway is a pure pass-through and nothing appears in the Logs tab.
    # The project is auto-created on first request if it doesn't exist.
    braintrust_project: str = "yaaos"

    # Full external origin of this backend deployment (scheme + host[:port], no
    # path), e.g. "https://app.yaaos.cloud". Single source for both the public
    # link base (`yaaos_app_base_url`) and the agent identity-exchange audience
    # (`yaaos_public_hostname`) — both derived below. Required; boot fails if unset.
    yaaos_public_origin: str = Field(
        ...,
        description="Full external origin (scheme+host[:port], no path), e.g. https://app.yaaos.cloud.",
    )

    # Time controls. Production defaults are reasonable; tests set short.
    # yaaos_review_debounce_seconds: int = 30
    yaaos_reaper_interval_seconds: int = 30
    yaaos_heartbeat_interval_seconds: int = 10

    # Session lifetime + cleanup cadence.
    yaaos_session_lifetime_seconds: int = 60 * 60 * 24 * 14  # 14 days
    yaaos_auth_cleanup_interval_seconds: int = 60 * 60  # 1 hour
    yaaos_integrations_health_check_interval_seconds: int = 60 * 60  # 1 hour
    yaaos_mcp_token_sweep_interval_seconds: int = 60 * 60  # 1 hour

    # Orphan-ticket sweep. A `running` ticket without any reviews row is the
    # tail of a webhook that didn't reach the reviewer (missing BYOK key,
    # crash mid-dispatch, etc.). The sweep flips such rows to `failed` so
    # the Dashboard "in flight" band drains correctly.
    yaaos_ticket_orphan_sweep_interval_seconds: int = 60
    yaaos_ticket_orphan_grace_seconds: int = 300  # 5 min

    # The platform yaaos GitHub App — used for per-org installs only
    # (app_id/private_key/webhook_secret drive installation-token minting + the
    # webhook receiver). The slug builds `${github_web_base_url}/apps/<slug>/installations/new`.
    # Required in `prod`; defaults let `dev` boot without provisioning. Tests
    # override at fixture time.
    yaaos_github_app_id: str = ""
    yaaos_github_app_slug: str = ""
    yaaos_github_app_private_key: SecretStr = SecretStr("")
    yaaos_github_app_webhook_secret: SecretStr = SecretStr("")
    # The platform yaaos GitHub *OAuth* App — used for "Sign in with GitHub"
    # only. This is a DIFFERENT GitHub primitive from the GitHub App above
    # (GitHub confusingly names them both): a GitHub OAuth App has no install
    # concept, no installation tokens, and authenticates with client_id /
    # client_secret to mint a user access token. Keeping the two registrations
    # separate means the install lifecycle and the login flow can fail (or be
    # disabled) independently.
    yaaos_github_oauth_client_id: str = ""
    yaaos_github_oauth_client_secret: SecretStr = SecretStr("")
    # The browser-facing authorize URL is always `<github_web_base_url>/login/oauth/authorize`.
    # The server-side token exchange URL is normally the same origin (real github.com
    # serves both), but the test stack splits the browser host (`localhost:58081`)
    # from the docker-internal host (`fake-github:8080`), so this override lets the
    # backend hit fake-github directly. Unset → defaults to web-base.
    yaaos_github_oauth_token_url: str = ""
    yaaos_oauth_state_secret: SecretStr = SecretStr("dev-only-oauth-state-secret")

    # TOTP master key (Fernet, 32 bytes URL-safe base64). Defaults to
    # empty; `core/identity.totp` falls back to `yaaos_encryption_key` when
    # unset so dev/test only need one key. Production must set this.
    yaaos_totp_master_key: SecretStr = SecretStr("")

    # Linear OAuth + hosted MCP. Defaults point at the real upstreams;
    # the test compose overrides to fake-linear hostnames.
    yaaos_oauth_linear_client_id: str = ""
    yaaos_oauth_linear_client_secret: SecretStr = SecretStr("")
    linear_oauth_authorize_url: str = "https://linear.app/oauth/authorize"
    linear_oauth_token_url: str = "https://api.linear.app/oauth/token"
    linear_oauth_refresh_url: str = "https://api.linear.app/oauth/token"
    linear_mcp_url: str = "https://mcp.linear.app/sse"
    linear_api_base_url: str = "https://api.linear.app"

    # Notion OAuth + hosted MCP. Same shape; Notion uses HTTP Basic
    # on the token endpoint, encoded in the provider config rather than here.
    yaaos_oauth_notion_client_id: str = ""
    yaaos_oauth_notion_client_secret: SecretStr = SecretStr("")
    notion_oauth_authorize_url: str = "https://api.notion.com/v1/oauth/authorize"
    notion_oauth_token_url: str = "https://api.notion.com/v1/oauth/token"
    notion_oauth_refresh_url: str = "https://api.notion.com/v1/oauth/token"
    notion_mcp_url: str = "https://mcp.notion.com/mcp"
    notion_api_base_url: str = "https://api.notion.com"

    # Invitations + dev SMTP (Mailpit).
    yaaos_invitation_token_secret: SecretStr = SecretStr("dev-only-invitation-secret")
    yaaos_invitation_lifetime_seconds: int = 60 * 60 * 24 * 7  # 7 days
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = "yaaos@localhost"
    smtp_use_tls: bool = False

    @property
    def is_non_prod(self) -> bool:
        """True when `yaaos_env` is `dev` or `test`. Use for affordances that
        should be permissive in both (NullPool, no-Secure cookies, etc.)."""
        return self.yaaos_env != "prod"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def yaaos_public_hostname(self) -> str:
        """Agent identity-exchange audience: host[:port], no scheme/path.
        `.netloc` (not `.hostname`) so a port survives — e.g. the test stack's
        `http://web:8080` → `web:8080`, matching the agent's `url.Host`."""
        return urlparse(self.yaaos_public_origin).netloc

    @computed_field  # type: ignore[prop-decorator]
    @property
    def yaaos_app_base_url(self) -> str:
        """Public base for emitted links (OAuth callbacks, invite/SAML/MCP URLs)."""
        return self.yaaos_public_origin.rstrip("/")

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
