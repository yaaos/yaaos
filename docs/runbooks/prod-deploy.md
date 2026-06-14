# Production deploy

Manual-config checklist + deploy flow + rollback for `app.yaaos.dev`. Grouped by provider, dependency-ordered. A teammate can stand up prod from scratch by working through every section in order.

## Config placement rule

- **`fly.production.toml [env]`** — non-secret config. Committed, visible via `fly config show`. Boot-required non-secrets live here so they can't be forgotten on a fresh deploy.
- **`fly secrets`** — credentials, keys, and tokens only. Encrypted, never in git.
- Never define the same var in both — a secret silently overrides an `[env]` value of the same name.
- Rule of thumb: credential / key / token → secret; everything else → `[env]`.

Required secrets that must exist **before the first deploy push** (absence crash-loops the machine): `DATABASE_URL`, `REDIS_URL`, `YAAOS_ENCRYPTION_KEY`, `YAAOS_PUBLIC_ORIGIN` (set in `[env]`, not secret — but required at boot).

---

## 1. Neon (Postgres)

- New project, region **AWS US East (Ohio)** — closest to Fly `ord`.
- Postgres **18** required — `migrate()` asserts `server_version_num ≥ 180000` and crash-loops otherwise.
- **Use the DIRECT (non-pooled) connection URL.** The host must have NO `-pooler` segment.

> **Critical footgun:** the pooled URL silently corrupts advisory-lock-based migrations. `pg_advisory_lock` requires a persistent connection; transaction pooling breaks that guarantee. Using the pooled URL will corrupt the migration state without an obvious error.

- URL conversion for asyncpg (the app passes `DATABASE_URL` straight into SQLAlchemy's asyncpg engine):
  - scheme `postgresql://` → `postgresql+asyncpg://`
  - replace `?sslmode=require&channel_binding=require` → `?ssl=require` (libpq-only params that asyncpg rejects)
  - percent-encode any of `@ : / ? # [ ] %` in the password
- Set the final URL as the `DATABASE_URL` Fly secret.

---

## 2. Upstash (Redis)

- New regional database, region **AWS us-east-1** (closest to Fly `ord`; no us-central on Upstash).
- **Eviction OFF** — the database backs the taskiq queue (`ListQueueBroker` Redis lists) + SSE pub/sub. Eviction silently drops queued jobs.
- Copy the **Redis-protocol TLS endpoint** (`rediss://default:<pass>@<host>.upstash.io:6379`), not the REST URL.
- Set as the `REDIS_URL` Fly secret. Percent-encode password special chars.

---

## 3. Core secrets (generated locally)

Generate with stdlib Python (no installs required):

| Fly secret | How to generate |
|---|---|
| `YAAOS_ENCRYPTION_KEY` | Fernet key: `python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` |
| `YAAOS_TOTP_MASTER_KEY` | Second distinct Fernet key (run the same command again) |
| `YAAOS_OAUTH_STATE_SECRET` | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `YAAOS_INVITATION_TOKEN_SECRET` | Second distinct `token_urlsafe(48)` |

- `YAAOS_CORS_ORIGINS` — leave unset. Backend and SPA share one origin; CORS is not needed.
- See [secret-rotation.md](secret-rotation.md) for blast-radius detail on each key.

---

## 4. Fly app

### App creation

- App name: `yaaos`, region: `ord`.
- Do not connect a GitHub repository — RWX deploys via `FLY_API_TOKEN`, no GitHub Actions.

### `[env]` block (in `fly.production.toml`)

Already committed. Key values:

| Var | Value |
|---|---|
| `APP_MODE` | `production` |
| `ENVIRONMENT` | `production` |
| `YAAOS_PUBLIC_ORIGIN` | `https://app.yaaos.dev` |
| `YAAOS_GITHUB_APP_ID` | (GitHub App numeric ID) |
| `YAAOS_GITHUB_APP_SLUG` | `yaaos` |
| `YAAOS_GITHUB_OAUTH_CLIENT_ID` | (OAuth App client ID) |

`APP_MODE=production` activates rate limiting, `Secure` cookies, and the prod-secrets gate. `ENVIRONMENT=production` is the OTel deployment tier. They are independent — never derive one from the other. See [core_config.md](../../apps/backend/docs/core_config.md).

### SMTP non-secret env

Set in the Fly dashboard env (not `[env]` block — avoids a redeploy to change):

| Var | Value |
|---|---|
| `SMTP_HOST` | `smtp.resend.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USERNAME` | `resend` |
| `SMTP_USE_TLS` | `true` |
| `SMTP_FROM` | `yaaos <noreply@yaaos.dev>` |

### Fly secrets

Set via the Fly dashboard Secrets UI:

| Secret | Source |
|---|---|
| `DATABASE_URL` | Neon direct URL (postgresql+asyncpg://…) |
| `REDIS_URL` | Upstash TLS endpoint (rediss://…) |
| `YAAOS_ENCRYPTION_KEY` | Generated in §3 |
| `YAAOS_TOTP_MASTER_KEY` | Generated in §3 |
| `YAAOS_OAUTH_STATE_SECRET` | Generated in §3 |
| `YAAOS_INVITATION_TOKEN_SECRET` | Generated in §3 |
| `YAAOS_GITHUB_OAUTH_CLIENT_SECRET` | GitHub OAuth App secret |
| `YAAOS_GITHUB_APP_PRIVATE_KEY` | GitHub App PEM (full multiline key) |
| `YAAOS_GITHUB_APP_WEBHOOK_SECRET` | GitHub App webhook secret |
| `SMTP_PASSWORD` | Resend API key (`re_…`) |
| `YAAOS_CLOUDFLARE_INGRESS_SECRET` | Shared token — see §6 (Cloudflare) |
| `YAAOS_BACKEND_DASH0_BEARER_TOKEN` | Dash0 bearer for backend OTLP export — see §7 |
| `YAAOS_AGENT_DASH0_BEARER_TOKEN` | Dash0 bearer for agent OTLP export — see §7 |

The two Dash0 bearers are `SecretStr | None` fields in `Settings` (`yaaos_backend_dash0_bearer_token`, `yaaos_agent_dash0_bearer_token`). See [core_config.md](../../apps/backend/docs/core_config.md).

### Process groups

`fly.production.toml` declares `web` and `worker` as separate Fly Machines, both from the same image. After the first deploy verify both exist: if only `web` is present, run `fly scale count web=1 worker=1` via the Fly dashboard or `flyctl`.

---

## 5. GitHub App

- **Webhook URL:** `https://app.yaaos.dev/api/intake/github` — set this after the first deploy.
- **OAuth callback URL:** `https://app.yaaos.dev/api/auth/github/callback`.
- App ID, OAuth client ID, and slug go in `fly.production.toml [env]` (visible identifiers — not secrets).
- Private key (`YAAOS_GITHUB_APP_PRIVATE_KEY`), webhook secret (`YAAOS_GITHUB_APP_WEBHOOK_SECRET`), and OAuth client secret (`YAAOS_GITHUB_OAUTH_CLIENT_SECRET`) go in Fly secrets.

---

## 6. Cloudflare

### DNS and TLS

- Add `fly certs add app.yaaos.dev` (via `flyctl ssh console` on the jump host) for the Fly-side certificate.
- Add a Cloudflare DNS record pointing `app.yaaos.dev` → the Fly app, with proxy **ON** (orange cloud).
- SSL/TLS mode: **Full (strict)**.

### Transform Rule (ingress enforcement)

Cloudflare injects a shared secret into every proxied request so the backend can reject traffic that bypasses Cloudflare entirely.

- Create a Transform Rule that adds the header `X-Yaaos-cf-Ingress: <secret>` to all requests to `app.yaaos.dev`. Do **not** use a `CF-*` header name — Cloudflare reserves the `CF-*` prefix for its own managed headers and Transform Rules reject `Set static` on that prefix.
- The same token value must be set as the `YAAOS_CLOUDFLARE_INGRESS_SECRET` Fly secret. The backend boot check (`_check_required_prod_secrets`) refuses to start with this unset in `production`, so a missed rotation step fails loudly instead of silently disabling ingress enforcement.
- **These two values must stay in sync.** Rotating the Cloudflare rule value without updating the Fly secret (or vice versa) causes every request to return 403 during the skew window. Rotation procedure: update both atomically (set new Fly secret → update Cloudflare rule → verify → remove old).
- CORS allowlist in Dash0 must include `https://app.yaaos.dev` with `Authorization` and `Dash0-Dataset` headers allowed — see §7.

### Worker health check access

The worker health server binds `0.0.0.0:8081`. Fly's machine checker reaches it directly inside the 6PN (private) network and **bypasses Cloudflare**, so the `X-Yaaos-cf-Ingress` check does not apply to health probe requests.

---

## 7. Dash0 (observability)

Two separate tokens are required — they have different scopes.

| Token | Scope | Usage |
|---|---|---|
| **Backend token** | Full-signal (traces + metrics + logs), org-scoped | `YAAOS_BACKEND_DASH0_BEARER_TOKEN` Fly secret |
| **Agent token** | Full-signal, org-scoped | `YAAOS_AGENT_DASH0_BEARER_TOKEN` Fly secret |
| **Browser token** | Web-signal restricted + dataset-scoped + ingest-only | `VITE_DASH0_AUTH_TOKEN` in the RWX `yaaos` vault |

### Backend OTLP configuration

Non-secret values in `fly.production.toml [env]`:
- `YAAOS_DASH0_ENDPOINT` — the Dash0 OTLP base URL (e.g. `https://ingress.us-west-2.aws.dash0.com`).
- `YAAOS_DASH0_DATASET` — dataset name (e.g. `default`).

Fly secrets (sensitive):
- `YAAOS_BACKEND_DASH0_BEARER_TOKEN` — backend bearer for OTLP export.
- `YAAOS_AGENT_DASH0_BEARER_TOKEN` — agent bearer forwarded to the agent via `ConfigUpdateCommand`.

All four must be set; any missing setting silently skips OTLP exporters.

### Browser OTLP configuration (RWX vault)

The SPA bakes `VITE_*` values at build time. Set these in the RWX `yaaos` vault:

| Vault secret | Purpose |
|---|---|
| `VITE_OTEL_COLLECTOR_ENDPOINT` | Dash0 OTLP endpoint for browser spans |
| `VITE_DASH0_AUTH_TOKEN` | Browser ingest-only token |
| `VITE_DASH0_DATASET` | Dash0 dataset name |

The `deploy-production` RWX task passes these as `--build-arg` values to `flyctl deploy`. See `.rwx/push.yml`.

### CORS

In the Dash0 dashboard, add a CORS allow entry for `https://app.yaaos.dev` with `Authorization` and `Dash0-Dataset` as allowed headers. Required for the browser to send OTLP spans directly to Dash0.

---

## 8. Resend (email)

- Domain `yaaos.dev` must be verified in Resend: add the SPF and DKIM records in Cloudflare DNS as **DNS-only (grey cloud)** — proxied records break email DNS validation.
- `SMTP_PASSWORD` Fly secret = the Resend API key (`re_…`). The email layer is vendor-neutral SMTP; the env var is `SMTP_PASSWORD`, not `RESEND_API_KEY`.
- **Port 465 + SSL only.** The email send path (`apps/backend/app/domain/orgs/email.py`) uses `SMTP_SSL` or plain `SMTP` — there is no `starttls()` call, so port 587/STARTTLS would send cleartext. Use port 465 with `SMTP_USE_TLS=true`.

---

## 9. Docker Hub (agent image)

- Docker Hub repository: `yaaos/agent`.
- Account credentials go in the RWX `yaaos` vault:

| Vault secret | Purpose |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub account username |
| `DOCKERHUB_TOKEN` | Docker Hub access token (push scope) |

The `publish-agent-image` RWX task reads these. See `.rwx/push.yml`.

---

## 10. RWX vault (`yaaos`)

All secrets the CI/deploy pipeline reads from the vault:

| Vault secret | Used by |
|---|---|
| `FLY_API_TOKEN` | `deploy-production` task |
| `DOCKERHUB_USERNAME` | `publish-agent-image` task |
| `DOCKERHUB_TOKEN` | `publish-agent-image` task |
| `YAAOS_CLOUDFLARE_INGRESS_SECRET` | must match the Cloudflare Transform Rule and the Fly secret |
| `VITE_OTEL_COLLECTOR_ENDPOINT` | `deploy-production` build arg |
| `VITE_DASH0_AUTH_TOKEN` | `deploy-production` build arg |
| `VITE_DASH0_DATASET` | `deploy-production` build arg |

`FLY_API_TOKEN` is the only credential RWX needs to deploy; it is org-scoped (not personal).

---

## Deploy flow

### Local validation

Before pushing to `main`, run all CI scripts:

- `apps/backend/bin/ci`
- `apps/web/bin/ci`
- `apps/agent/bin/ci`
- `apps/e2e/bin/ci` (requires the Docker stack — `bin/dev-rebuild` first)

### Automated deploy on push to `main`

Push to `main` → RWX triggers `push.yml`:

1. `ci-docs`, `ci-backend`, `ci-web`, `ci-agent`, `ci-e2e` run in parallel.
2. `deploy-production` fires only when **all five** pass **and** the push touched deploy-relevant files — it carries a `filter:` block (`apps/backend/**`, `apps/web/**`, the Python/pnpm workspace manifests, and `fly.production.toml`). An agent-only push (`apps/agent/**`) skips `deploy-production` (its image ships via `publish-agent-image`); a doc-only push skips deployment entirely. The `filter:` list in `.rwx/push.yml` is the canonical source.
3. `flyctl deploy --remote-only --config fly.production.toml` builds the amd64 image on Fly's remote builder and deploys.

### Bluegreen cutover

Strategy is `bluegreen` (declared in `fly.production.toml`). Fly:

1. Boots new machines for every process group.
2. Gates cutover on passing health checks across all groups — `/api/health` for `web`, `/health` on port 8081 for `worker`.
3. Shifts traffic to the new machines.
4. Drains the old machines over the `kill_timeout` (180s — covers worker drain 60s + uvicorn graceful shutdown 30s + OTel flush ~90s).

Both groups must pass their health check before cutover proceeds. A failing health check blocks the deploy — it does not roll back automatically.

### First-time bootstrap (after first deploy)

Runs once after migrations complete:

1. `fly ssh console` (via `flyctl` on the RWX jump host).
2. `apps/backend/bin/bootstrap`
3. Five interactive prompts: email, GitHub username, display name, org name, org slug.
4. Idempotent — a second run with the same inputs is a no-op.

### CSP report-only → enforce

`YAAOS_CSP_MODE` defaults to `report-only` — the backend emits `Content-Security-Policy-Report-Only`, the browser logs violations to DevTools but doesn't block resources. After the first prod deploy:

1. Load the SPA in Chrome with DevTools → Console open. Click through the main flows (dashboard, settings, ticket detail).
2. Filter Console for "Refused" / "Content Security Policy" — every violation logs there.
3. If clean: `fly secrets set YAAOS_CSP_MODE=enforce --app yaaos` to promote to enforcing mode. (Or set in `fly.production.toml [env]` — it's not a secret.)
4. If a directive is too tight: edit `apps/backend/app/core/webserver/csp.py` to add the host, ship, repeat from step 1.

The CSP policy itself lives in `csp.py` as a constant — no per-route customization. Adding a host means changing that file.

### Agent image publish

The WorkspaceAgent image publishes separately from the backend deploy:

- `apps/agent/VERSION` contains the major version integer (bump for breaking changes).
- Merging a change under `apps/agent/**` to `main` triggers `publish-agent-image`.
- The task derives the next minor by querying existing Docker Hub tags for the current major, tags `yaaos/agent:MAJOR.MINOR` and `yaaos/agent:latest`, and pushes.
- Users pin `yaaos/agent:MAJOR.MINOR` (e.g. `yaaos/agent:0.1`).
- Bump `MAJOR` in `apps/agent/VERSION` only for breaking wire-protocol changes.

---

## Rollback

### Image rollback

Two equivalent approaches via `flyctl`:

- `flyctl releases rollback` — interactive; picks the prior release.
- `flyctl deploy --image <prior-digest>` — explicit; use when you need a specific version.

### Schema rule during the bluegreen overlap window

Only **additive** migrations (new table, new nullable column, new index) are safe during the overlap window when old and new machines share the database. Destructive or non-additive schema changes must be staged: add the column in one deploy, remove the old one in a later deploy after all machines run the new code.

### Verification after rollback

- `https://app.yaaos.dev/api/health` returns 200.
- Worker health: `fly ssh console` → `curl http://localhost:8081/health`.
- Log in via the browser; trigger a test PR webhook if possible.
