# core/config

> Boot-time configuration via pydantic-settings — env vars and `.env*` files into a single typed `Settings`.

## Scope

- Owns: `Settings` (pydantic `BaseSettings`), `get_settings()` cached singleton.
- Read-only and stateless. No HTTP routes, no tables.

## Why / invariants

**Required fields raise at construction** — `database_url`, `yaaos_encryption_key`, `redis_url`, `yaaos_public_origin` must be set; absence crashes boot immediately with a pydantic `ValidationError`.

**All sensitive fields are `SecretStr`** — `repr`, `model_dump`, and `model_dump_json` all render as `'**********'`. Call `.get_secret_value()` only at the byte boundary (Fernet construction, JWT sign, HTTP Authorization header). Verified by `test_secret_redaction.py`.

**`.env` file precedence** — `.env` → `.env.local` → `.env.dev` → `.env.dev.local`. Later overrides earlier; process env overrides all. `extra="ignore"` so unknown vars don't error.

**Cached singleton** — `Settings()` parses env on every call; `@cache` on `get_settings()` makes subsequent calls free. Tests monkeypatching env must call `get_settings.cache_clear()` afterward.

**`YAAOS_PUBLIC_ORIGIN`** — required. Full external origin of this backend deployment (scheme + host[:port], no path; e.g. `https://app.yaaos.cloud`). Boot fails with a `ValidationError` when unset. Two values derive from it as `computed_field` properties (so existing readers are unchanged): `yaaos_app_base_url` = the origin (public link base for OAuth callbacks, invitation/SAML/MCP URLs), and `yaaos_public_hostname` = its `netloc` (host[:port]), which `core/agent_gateway` validates against the `X-Yaaos-Audience` header. The derived hostname must match `hostFromURL(YAAOS_BACKEND_URL)` on the agent side — that's `url.Host`, so a port is preserved (e.g. `web:8080`); use `.netloc`, not `.hostname`.

**`APP_MODE` vs `ENVIRONMENT` — two orthogonal axes.**

- `APP_MODE` (field `app_mode`, `Literal["dev","test","production"]`, default `"production"`) is a **behavior switch**: `dev` enables permissive CORS and NullPool; `test` enables the test OAuth stub and routes emails to the in-memory inbox; `production` activates rate limiting, `Secure` cookies, and the prod-secrets gate.
- `ENVIRONMENT` (field `environment`, `Literal["local","development","staging","production"]`, default `"local"`) is the **deployment tier** for telemetry (`deployment.environment.name`). A staging deploy runs `APP_MODE=production` (prod-like behavior) but `ENVIRONMENT=staging` (distinct OTel dataset). They are never derived from each other.

**Canonical accessors** — the literal string `"production"` appears in exactly one file (`service.py`). Call-sites use the properties:

- `is_production` — `app_mode == "production"`
- `is_dev` — `app_mode == "dev"`
- `is_test` — `app_mode == "test"`
- `is_non_prod` — `app_mode != "production"` (shorthand for dev + test together)
- `cors_origins_list` returns `["*"]` when `is_non_prod`; otherwise parsed `YAAOS_CORS_ORIGINS`.

**`YAAOS_CLOUDFLARE_INGRESS_SECRET`** — shared secret injected by a Cloudflare Transform Rule into the `CF-Access-Yaaos-Ingress` header on every proxied request. `CloudflareIngressMiddleware` reads this at request time (not boot time) and rejects mismatches with 403. Empty default = no-op so dev/test/e2e are unaffected; in `production` `_check_required_prod_secrets` refuses to boot if unset, so the no-op branch is unreachable in prod. Set as a Fly secret in production.

**`SERVICE_VERSION`** — `service_version: str = "0.0.0-dev"`. Version string embedded in the OTel resource (`service.version`) and served at `/api/health`. Set by the deploy pipeline (e.g. git SHA or semver tag). Default is a safe sentinel for local/dev boots.

**OTLP auth headers — no Settings field.** `OTEL_EXPORTER_OTLP_HEADERS` (`Authorization=Bearer <token>,Dash0-Dataset=<name>`) is set as a Fly secret and read by the OTel SDK directly at exporter construction time. Nothing in our code parses it. Exporter wiring is gated on `otel_exporter_otlp_endpoint` being set; the SDK then reads the standard OTLP env vars.

**`YAAOS_WORKER_HEALTH_PORT`** — `yaaos_worker_health_port: int = 8081`. TCP port the worker health server binds on `0.0.0.0`. The Fly `[[services]]` check for the `worker` process group targets this port. Default 8081 is out of the way of the web process (8080) and is not publicly routed — Fly's machine checker reaches it directly inside the 6PN (private) network, bypassing Cloudflare.

## Gotchas

- Callers never instantiate `Settings` directly — always via `get_settings()`.
- `APP_MODE` and `ENVIRONMENT` are independent. Never derive one from the other.
- The Dockerfile sets `APP_MODE=production` at image build time; tests in `conftest.py` override it to `test` before any import.
- Tests that override `YAAOS_CLOUDFLARE_INGRESS_SECRET` must call `get_settings.cache_clear()` before and after — the cached singleton won't pick up the env change otherwise.
- `OTEL_EXPORTER_OTLP_HEADERS` is never a Settings field — it's a standard OTel env var consumed directly by the SDK. Do not add a `SecretStr` field for it; auth headers must not cross the Python module boundary.
