# Setup

> How to get yaaos running. Covers the Docker stack (recommended) and the no-Docker local dev path.

## Prerequisites

- Docker + Docker Compose v2.
- A GitHub account with permission to create GitHub Apps on the org you want reviewed.
- An Anthropic API key (yaaos shells out to the Claude Code CLI).
- For local webhook testing: a smee.io channel URL.

## 1. Clone and configure

- Clone the repo.
- Copy `.env.sample` to `.env`.
- Generate the at-rest encryption key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` → paste into `.env` as `YAAOS_ENCRYPTION_KEY`. Persist out-of-band — losing it forces every operator to re-enter credentials via the Settings UI.
- Leave `DATABASE_URL` at the default; compose provisions Postgres with matching credentials.
- Optional: set `OTEL_EXPORTER_OTLP_ENDPOINT` for span export. Unset → OTel disabled.

Full env-var reference: [`apps/backend/docs/core_config.md`](../apps/backend/docs/core_config.md).

### Key env vars

| Var | Purpose |
|---|---|
| `YAAOS_ENCRYPTION_KEY` | Fernet, 32-byte URL-safe base64. Encrypts plugin credentials + (fallback) TOTP/SAML keys. |
| `YAAOS_TOTP_MASTER_KEY` | Fernet, 32-byte URL-safe base64. Encrypts TOTP secrets + SP private keys. Falls back to `YAAOS_ENCRYPTION_KEY` in non-prod. |
| `YAAOS_OAUTH_STATE_SECRET` | itsdangerous secret for OAuth `state`, TOTP-challenge, GitHub-install state, SAML stub assertions. Rotate on suspected compromise. |
| `YAAOS_INVITATION_TOKEN_SECRET` | itsdangerous secret for invitation tokens (7-day TTL). |
| `YAAOS_GITHUB_APP_ID` / `_SLUG` / `_PRIVATE_KEY` / `_WEBHOOK_SECRET` | Platform GitHub App — per-org installs + webhook receiver. |
| `YAAOS_GITHUB_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` | Platform GitHub OAuth App — "Sign in with GitHub" only. |
| `YAAOS_APP_BASE_URL` | Public origin. Used in invitation + SAML ACS URLs. |
| `SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` / `_FROM` / `_USE_TLS` | Outbound mail. Dev → Mailpit (`localhost:1025`). |
| `YAAOS_SESSION_LIFETIME_SECONDS` | Session cookie lifetime; default 14 days. |
| `YAAOS_AUTH_CLEANUP_INTERVAL_SECONDS` | Cleanup loop tick; default 1h. Purges expired sessions + invitations + audit rows older than `AUDIT_LOG_RETENTION` (30d). |

The backend refuses to start in `prod` with any required secret unset; dev/test boot with stub defaults.

### Linear + Notion OAuth (optional)

Hosted-MCP integrations for Linear and Notion. The test suite runs against in-tree fakes — no real OAuth apps needed for end-to-end testing.

- **Linear:** register at <https://linear.app/settings/api> → OAuth applications. Scopes: `read`. Callback: `https://<your-domain>/api/mcp-proxy/linear/callback`. Vars: `YAAOS_OAUTH_LINEAR_CLIENT_ID` / `YAAOS_OAUTH_LINEAR_CLIENT_SECRET`.
- **Notion:** register at <https://notion.so/my-integrations> as a Public integration. Capabilities: read content + comments + user info. Callback: `https://<your-domain>/api/mcp-proxy/notion/callback`. Vars: `YAAOS_OAUTH_NOTION_CLIENT_ID` / `YAAOS_OAUTH_NOTION_CLIENT_SECRET`.

Provider URL vars (defaults point at real upstreams; test compose overrides to fakes): `LINEAR_OAUTH_AUTHORIZE_URL`, `LINEAR_OAUTH_TOKEN_URL`, `LINEAR_OAUTH_REFRESH_URL`, `LINEAR_MCP_URL`, and Notion equivalents.

### Dev mail (Mailpit)

Dev compose starts [Mailpit](https://mailpit.axllent.org/) — local SMTP sink. Web UI: <http://localhost:8025>. No real mail sent in dev.

## 1b. Bootstrap the first user + org

A fresh database is anonymous. Run once:

```bash
apps/backend/bin/bootstrap
```

Five prompts: email, GitHub username, display name, org name, org slug. Resolves GitHub username to stable numeric ID via `GET https://api.github.com/users/<login>`. Idempotent.

After bootstrap, sign in via "Sign in with GitHub". Bootstrap is mandatory: OAuth never auto-provisions a yaaos user. Signing in without a pre-existing user redirects to `/login?reason=not_provisioned`. Teammates join via the email-invitation flow.

## 2. Bring up the stack

```bash
bin/dev-rebuild   # Postgres + Redis + backend (API on :8080 + bundled SPA) + worker + Mailpit + WorkspaceAgent
```

Visit `http://localhost:8080`. Dashboard renders the onboarding stepper until GitHub App + Anthropic key are configured.

### Workers

`apps/backend/app/worker.py` runs taskiq workers + outbox drain. Local dev uses in-memory `WorkspaceProvider` — no Go `apps/agent/` container required.

### Running the WorkspaceAgent locally

The dev compose overlay includes an `agent` service (placeholder identity-exchange verifier accepts any non-empty `YAAOS_SIGNED_STS_REQUEST`):

```bash
docker compose -f docker/docker-compose.dev.yml --env-file .env up -d --build agent
```

### WebSocket activity stream

Behind ALB / nginx, configure `--ws-ping-interval=30 --ws-ping-timeout=10` on uvicorn so idle WebSocket connections survive proxy idle-timeouts. Local dev uses uvicorn defaults — agent reconnect loop covers drops.

## 3. Provision the two GitHub registrations

yaaos requires **two distinct GitHub-side registrations** per deployment. Customers don't bring their own.

### 3a. GitHub App (per-org installs)

1. <https://github.com/settings/apps/new>
2. Configure:
   - **Homepage URL:** your deployment URL.
   - **Setup URL:** `<deployment>/api/github/install_callback`; check "Redirect on update."
   - **Webhook URL:** `<deployment>/api/intake/github` (prod) or smee channel (laptop dev).
   - **Webhook secret:** high-entropy string; keep it.
   - **Repository permissions:** Contents (read), Pull requests (write), Metadata (read), Issues (write).
   - **Subscribe to events:** Pull request, Pull request review comment, Issue comment, Installation.
   - Leave "User authorization callback URL" blank — sign-in does **not** go through this App.
3. After saving: grab App ID, slug, and generated PEM.
4. Into `.env`: `YAAOS_GITHUB_APP_ID`, `YAAOS_GITHUB_APP_SLUG`, `YAAOS_GITHUB_APP_PRIVATE_KEY`, `YAAOS_GITHUB_APP_WEBHOOK_SECRET`.
   - Private key: paste PEM as one line with literal `\n`. Conversion: `awk 'NR>1{printf"\\n"}{printf"%s",$0}' yaaos-dev.YYYY-MM-DD.private-key.pem`.

### 3b. GitHub OAuth App (sign-in)

1. <https://github.com/settings/developers> → **New OAuth App**
2. Configure:
   - **Homepage URL:** your deployment URL.
   - **Authorization callback URL:** `<deployment>/api/auth/callback/github`.
3. Generate a client secret.
4. Into `.env`: `YAAOS_GITHUB_OAUTH_CLIENT_ID`, `YAAOS_GITHUB_OAUTH_CLIENT_SECRET`.

Restart the backend. Customers install the GitHub App via **Org Settings > VCS > Install yaaos on GitHub**.

## 4. Set the Anthropic API key

In the dashboard's Model API key card, paste the Anthropic key and Save. yaaos probes `GET /v1/models` to verify; badge stays red until the probe succeeds. The key is encrypted at rest — never written to a workspace, never logged, never in audit payloads.

## 5. First review

Open a PR on a repo the App can see (not a draft, not a fork). yaaos receives `pull_request.opened`, creates a ticket, provisions a workspace, invokes Claude Code. The Tickets page shows live SSE updates (`queued → running → posted`).

## Local dev — Docker stack (recommended)

Bind-mount source into containers; edits take effect on next restart. Auto-reload intentionally off — manual restart is ~2-3s.

```bash
pnpm --filter ./apps/web build   # one-time
bin/dev-rebuild                  # builds image + starts stack
```

`bin/dev-restart` — env-only variant (no rebuild); use after `.env` edits.

| Edit | Action | Time |
|---|---|---|
| Backend `.py` | `docker compose --env-file .env restart app` | ~2s |
| Web `.tsx` | `pnpm --filter ./apps/web build` + refresh | ~5-10s |
| Web `.tsx` (auto) | `pnpm exec vite build --watch` in separate terminal | ~1-2s |
| Deps change | `docker compose … up -d --build app` | 30-60s |

`apps/web/dist/` is bind-mounted and served by FastAPI's `StaticFiles` — FE rebuilds refresh without restarting the backend. Dev compose sets `YAAOS_ENV=dev` (switches to `NullPool`, enables `/api/testing/*` routes).

## Local dev — fully native (no Docker for app/web)

- **Postgres:** `docker compose -f docker/docker-compose.dev.yml --env-file .env up -d postgres` or native install.
- **Backend:** `cd apps/backend && uv sync && uv run uvicorn app.web:app --reload --port 8080`
- **Frontend:** `cd apps/web && pnpm install && pnpm dev` (Vite on `:5173`, proxies `/api/*` to `:8080`)
- **Claude Code CLI:** `npm install -g @anthropic-ai/claude-code` or set `YAAOS_CODING_AGENT_STUB=1` for deterministic stub responses.

## Test stack

Self-contained: Postgres + `apps/fake-github` + backend with stubbed coding agent. Run `apps/e2e/bin/ci` for up → run → down. See [`apps/e2e/docs/README.md`](../apps/e2e/docs/README.md).

## Claude Code Stop hook

`.claude/settings.json` registers a Stop hook that runs the relevant `apps/<app>/bin/ci` before a Claude turn ends. Fires only for non-`.md` changes under `apps/backend/` or `apps/web/`. e2e intentionally excluded — expensive and Docker-dependent. The hook is a safety net; run `bin/ci` yourself during work.

## Live API reference

`http://localhost:8080/docs` — Swagger UI. `http://localhost:8080/redoc` — reference rendering. Generated from the live OpenAPI schema.
