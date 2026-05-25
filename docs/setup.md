# Setup

How to get yaaos running. Covers the Docker stack (recommended) and the no-Docker local dev path.

## Prerequisites

- Docker + Docker Compose v2 (for the standard path).
- A GitHub account with permission to create GitHub Apps on the org you want reviewed.
- An Anthropic API key (yaaos shells out to the Claude Code CLI).
- For local-dev webhook testing: a smee.io channel URL (free; relays GitHub webhooks to your laptop).

## 1. Clone and configure

- Clone the repo.
- Copy `.env.sample` to `.env`.
- Generate the at-rest encryption key (32 bytes URL-safe base64) and paste it into `.env` as `YAAOS_ENCRYPTION_KEY`. Recipe: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Persist this key out-of-band (1Password, secrets manager) — losing it forces every operator to re-enter their credentials via the Settings UI.
- Leave `DATABASE_URL` at the default; the compose stack provisions Postgres with matching credentials (`yaaos:yaaos@postgres:5432/yaaos`).
- Optional: set `OTEL_EXPORTER_OTLP_ENDPOINT` if you want spans exported. Unset → OTel disabled silently.

The full env-var list is in [`apps/backend/docs/core_config.md`](../apps/backend/docs/core_config.md).

### GitHub OAuth

Dev login uses a real GitHub **OAuth App** (distinct from the GitHub App used for installs) — credentials provisioned out-of-band and pasted into `.env` as `YAAOS_GITHUB_OAUTH_CLIENT_ID` + `YAAOS_GITHUB_OAUTH_CLIENT_SECRET`. The callback URL is the dev origin's `/api/auth/callback/github`. Production uses its own OAuth App.

### env vars (full inventory)

Required in prod; defaults shipped for dev/test:

| Var | Purpose |
|---|---|
| `YAAOS_ENCRYPTION_KEY` | Fernet, 32-byte URL-safe base64. Encrypts plugin credentials + (fallback) TOTP/SAML keys. |
| `YAAOS_TOTP_MASTER_KEY` | Fernet, 32-byte URL-safe base64. Encrypts TOTP secrets + SP private keys. Falls back to `YAAOS_ENCRYPTION_KEY` in non-prod. |
| `YAAOS_OAUTH_STATE_SECRET` | itsdangerous secret for OAuth `state`, TOTP-challenge, GitHub-install state, SAML stub assertions. Rotate on suspected compromise. |
| `YAAOS_INVITATION_TOKEN_SECRET` | itsdangerous secret for invitation tokens (7-day TTL). |
| `YAAOS_GITHUB_APP_ID` / `YAAOS_GITHUB_APP_SLUG` / `YAAOS_GITHUB_APP_PRIVATE_KEY` / `YAAOS_GITHUB_APP_WEBHOOK_SECRET` | The platform yaaos **GitHub App**. Drives per-org installs + the webhook receiver. Provisioning instructions below. |
| `YAAOS_GITHUB_OAUTH_CLIENT_ID` / `YAAOS_GITHUB_OAUTH_CLIENT_SECRET` | The platform yaaos **GitHub OAuth App** — a distinct GitHub primitive. Drives "Sign in with GitHub" only. Provisioning instructions below. |
| `YAAOS_APP_BASE_URL` | Public origin of this deployment. Used in invitation + SAML ACS URLs. |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_USE_TLS` | Outbound mail (invitations). Dev → Mailpit (`localhost:1025`). |
| `YAAOS_SESSION_LIFETIME_SECONDS` | Session cookie lifetime; default 14 days. |
| `YAAOS_AUTH_CLEANUP_INTERVAL_SECONDS` | Cleanup loop tick; default 1h. Purges expired sessions + invitations + audit (`AUDIT_LOG_RETENTION` = 30d). |

The backend refuses to start in `prod` with any *required* secret unset; dev/test boot with stub defaults.

### Linear + Notion OAuth (— optional)

adds hosted-MCP integrations for Linear and Notion. The autonomous test suite runs against the in-tree `apps/fake-linear` and `apps/fake-notion` fakes — no real OAuth apps are required to ship end-to-end. You only need to register real apps when you want to use yaaos against production Linear / Notion data.

**Linear OAuth App** — register at <https://linear.app/settings/api> → OAuth applications. Scopes: `read`. Production callback at `https://<your-domain>/api/mcp-proxy/linear/callback`. Drop `client_id` + `client_secret` into `.env` as `YAAOS_OAUTH_LINEAR_CLIENT_ID` / `YAAOS_OAUTH_LINEAR_CLIENT_SECRET`.

**Notion OAuth App** — register at <https://notion.so/my-integrations> as a **Public integration**. Capabilities: read content + read comments + read user info. Production callback at `https://<your-domain>/api/mcp-proxy/notion/callback`. Drop credentials into `.env` as `YAAOS_OAUTH_NOTION_CLIENT_ID` / `YAAOS_OAUTH_NOTION_CLIENT_SECRET`.

also adds these provider URL env vars (defaults point at the real upstreams; test compose overrides to the fakes):

| Var | Default |
|---|---|
| `LINEAR_OAUTH_AUTHORIZE_URL` | `https://linear.app/oauth/authorize` |
| `LINEAR_OAUTH_TOKEN_URL` | `https://api.linear.app/oauth/token` |
| `LINEAR_OAUTH_REFRESH_URL` | `https://api.linear.app/oauth/token` |
| `LINEAR_MCP_URL` | (Linear-published hosted MCP endpoint) |
| `NOTION_OAUTH_AUTHORIZE_URL` / `NOTION_OAUTH_TOKEN_URL` / `NOTION_OAUTH_REFRESH_URL` / `NOTION_MCP_URL` | Notion equivalents |

### Dev mail (Mailpit)

The dev stack (`docker-compose.dev.yml`) starts [Mailpit](https://mailpit.axllent.org/) — a local SMTP sink that catches invitation emails and any other outbound mail. Web UI at <http://localhost:8025>. SMTP on `:1025`; backend points `SMTP_HOST`/`SMTP_PORT` there in dev. No real mail is ever sent in dev.

## 1b. Bootstrap the first user + org

A fresh database is anonymous. Run the bootstrap script once to mint the first user, link them to a GitHub identity, create the first org, and grant them the Owner role:

```bash
apps/backend/bin/bootstrap
```

Five prompts: your email, your GitHub username, your display name, the org name, the org slug (URL-safe). The script resolves the username to GitHub's stable numeric id via `GET https://api.github.com/users/<login>` and writes everything in one transaction. Idempotent — re-running with the same inputs prints `<row>=exists` instead of erroring.

After bootstrap, sign in via the "Sign in with GitHub" button on the login page. Bootstrap is mandatory: OAuth never auto-provisions a yaaos user. Signing in without a pre-existing user — matched either by `(provider, external_subject)` or by verified email — redirects you back to `/login?reason=not_provisioned`. New teammates join via `/api/memberships/accept` (the email-invitation flow).

## 2. Bring up the stack

From the repo root:

- `bin/dev-rebuild` brings up Postgres + Redis + the yaaos backend (which serves the API on `:8080` and the bundled SPA) plus the worker, Mailpit, and the local WorkspaceAgent. Equivalent to `docker compose -f docker/docker-compose.dev.yml --env-file .env up -d --build`.
- Visit `http://localhost:8080`. The dashboard renders the onboarding stepper because no GitHub App is installed and no Anthropic key is set.

### Workers

A separate worker process (`apps/backend/bin/worker`) runs taskiq workers + the outbox drain in a single Python process against Redis. Local dev uses the in-memory `WorkspaceProvider` so no Go `apps/agent/` container is required; remote-agent provisioning is exercised in the test stack only.

#### Running the WorkspaceAgent locally

The dev compose overlay ships an `agent` service that talks to the backend over the docker network using the placeholder identity-exchange verifier (any non-empty `YAAOS_SIGNED_STS_REQUEST` satisfies it):

```bash
docker compose \
    -f docker/docker-compose.dev.yml \
    --env-file .env up -d --build agent
```

The agent's first long-poll lands at the backend; from then on it heartbeats every 30s and waits for AgentCommands. `agent supervisor` runs the long-poll loop; `agent workspace` runs the per-workspace child-process dispatcher.

#### WebSocket activity stream

The agent opens `WSS /api/v1/agents/{id}/activity` after identity exchange. Behind ALB / nginx, configure `--ws-ping-interval=30 --ws-ping-timeout=10` on uvicorn so idle WebSocket connections survive proxy idle-timeouts (typically 60s). Local dev uses uvicorn defaults — the agent reconnect loop covers any drops.

## 3. Provision the two GitHub registrations

yaaos uses **two distinct GitHub-side registrations**, both provisioned once per yaaos deployment. Customers don't bring their own:

- A **GitHub App** — drives per-customer-org install + the webhook deliverability contract.
- A **GitHub OAuth App** — drives "Sign in with GitHub" for yaaos users.

GitHub names them confusingly. They are not interchangeable; do not paste GitHub App credentials into the OAuth slots or vice versa.

### 3a. GitHub App (per-org installs)

1. Visit <https://github.com/settings/apps/new> (or your GitHub org's Apps page if you want the App owned by an org).
2. Configure:
   - **Homepage URL:** your yaaos deployment URL.
   - **Setup URL:** `<deployment>/api/github/install_callback` and check "Redirect on update."
   - **Webhook URL:**
     - Production: `<deployment>/api/intake/github`.
     - Laptop dev: smee channel URL (`smee --url https://smee.io/<your-channel> --target http://localhost:8080/api/intake/github`).
   - **Webhook secret:** generate a high-entropy string; keep it.
   - **Repository permissions:** Contents (read), Pull requests (write), Metadata (read), Issues (write — for top-level PR comments).
   - **Subscribe to events:** Pull request, Pull request review comment, Issue comment, Installation.
   - Leave the "User authorization callback URL" blank — sign-in does **not** go through this App.
3. After saving, GitHub gives you the App ID, slug, and (after clicking "Generate a private key") a PEM.
4. Drop those values into your `.env`:
   - `YAAOS_GITHUB_APP_ID`, `YAAOS_GITHUB_APP_SLUG`, `YAAOS_GITHUB_APP_PRIVATE_KEY`, `YAAOS_GITHUB_APP_WEBHOOK_SECRET`.
   - **Private key:** paste the PEM as one line with literal `\n` between rows. The backend normalizes them to real newlines before signing. Single-shot conversion: `awk 'NR>1{printf"\\n"}{printf"%s",$0}' yaaos-dev.YYYY-MM-DD.private-key.pem`.

### 3b. GitHub OAuth App (sign-in)

1. Visit <https://github.com/settings/developers> → **New OAuth App** (or the equivalent org page).
2. Configure:
   - **Homepage URL:** your yaaos deployment URL.
   - **Authorization callback URL:** `<deployment>/api/auth/callback/github`.
3. After saving, click "Generate a new client secret".
4. Drop the values into your `.env`:
   - `YAAOS_GITHUB_OAUTH_CLIENT_ID`, `YAAOS_GITHUB_OAUTH_CLIENT_SECRET`.

Restart the backend. Sign-in and per-org install now work independently against their own registrations.

Customers (and your own org) install the GitHub App by signing in to yaaos as an Owner, opening **Org Settings > VCS**, clicking **Install yaaos on GitHub**, and choosing which repos to enable on github.com's install picker.

## 4. Set the Anthropic API key

- On the dashboard's Model API key card, paste your Anthropic key and Save. yaaos probes `GET /v1/models` against `api.anthropic.com` to verify the key authenticates; the badge stays red until the probe succeeds. A typo or revoked key keeps the dashboard in onboarding state.

The key is encrypted at rest with the Fernet key from your `.env`. It's never written to a workspace, never logged, and never echoed in audit payloads.

## 5. First review

- Open a PR on a repo the App can see (not a draft, not a fork).
- yaaos receives the `pull_request.opened` webhook, creates a ticket, schedules one review run, provisions a workspace, and invokes the Claude Code CLI. The parent reviewer dispatches yaaos-* subagents (architecture, security, line-level, tests, docs, conditional skill) via the Task tool, synthesizes their findings, and posts one Review back to GitHub.
- The Tickets page in the UI shows the ticket with live SSE updates as the job transitions `queued → running → posted`.

## Local dev — Docker stack (recommended)

Bind-mount the source into the running containers so edits take effect on the next restart — no image rebuild needed. Auto-reload is intentionally NOT used: mid-edit saves on a multi-file change would otherwise crash uvicorn on each broken import. Manual restart is ~2-3s and the logs stay quiet.

Bring it up:

```bash
pnpm --filter ./apps/web build         # one-time, populates apps/web/dist
bin/dev-rebuild                        # builds the image + starts the stack
```

`bin/dev-restart` is the env-only variant (no rebuild) — use it after `.env` edits. Both wrap the same `docker compose -f docker/docker-compose.dev.yml --env-file .env up -d [--build] app` invocation.

Inner loop:

| Edit              | Action                                          | Time   |
| ----------------- | ----------------------------------------------- | ------ |
| Backend `.py`     | `docker compose --env-file .env restart app`    | ~2s    |
| Web `.tsx`        | `pnpm --filter ./apps/web build` + refresh browser | ~5-10s |
| Web `.tsx` (auto) | `pnpm exec vite build --watch` in a separate terminal | ~1-2s per save |
| Deps change       | `docker compose … up -d --build app`            | 30-60s |

`apps/web/dist/` is bind-mounted into the container and served by FastAPI's `StaticFiles` per-request — FE rebuilds refresh in the browser without restarting the backend. The dev compose sets `YAAOS_ENV=dev`, which switches the engine to `NullPool` and enables the `/api/testing/*` reset+seed routes for local iteration.

The `app` and `worker` services build the same `web` / `worker` Dockerfile stages prod will use — dev image content matches prod. Real production is not docker-compose-based; the compose file is local only.

## Local dev — fully native (no Docker for app/web)

For iterating on the FE / BE without any container:

- **Postgres** — either run `docker compose -f docker/docker-compose.dev.yml --env-file .env up -d postgres` (just the DB), or use a native install. Match `DATABASE_URL` in `.env`.
- **Backend** — from `apps/backend/`: `uv sync` then `uv run uvicorn app.main:app --reload --port 8080`. The reloader picks up Python changes. Logs go to stdout in the terminal you ran it from.
- **Frontend** — from `apps/web/`: `pnpm install` then `pnpm dev`. Vite serves the SPA on `:5173` and proxies `/api/*` to `:8080`.
- **Claude Code CLI** — install on the host (`npm install -g @anthropic-ai/claude-code`) or skip by setting `YAAOS_CODING_AGENT_STUB=1`, which swaps in deterministic stub responses (no real LLM calls).

## Test stack

The e2e suite has its own self-contained stack — Postgres + `apps/fake-github` + yaaos backend with stubbed coding agent. Run `apps/e2e/bin/ci` for the one-shot up → run → down cycle. See [`apps/e2e/docs/README.md`](../apps/e2e/docs/README.md).

## Claude Code Stop hook

`.claude/settings.json` registers a Stop hook (`.claude/hooks/stop-ci.sh`) that runs the relevant `apps/<app>/bin/ci` before letting a Claude turn end. It diffs the working tree vs HEAD and only fires for non-`.md` changes under `apps/backend/` or `apps/web/`; pure-doc and infra-only turns skip CI for free. e2e is intentionally not in the hook — it's expensive and Docker-dependent. The hook is a safety net for the rule in [`CLAUDE.md`](../CLAUDE.md) § Implementation discipline: run `bin/ci` yourself, don't lean on the hook.

## Live API reference

When the backend is running, `http://localhost:8080/docs` is interactive Swagger UI and `http://localhost:8080/redoc` is the reference-style rendering. Both are generated from the live OpenAPI schema; they're the authoritative endpoint catalogue.

## Production considerations (deferred)

yaaos is currently a POC. The standard production-hardening items (auth + RBAC, rate limiting, security headers, real workspace isolation via containers or VMs, CLI agent retention pinning, audit-log pruning) are tracked in [`plan/`](../plan/). Read [`docs/system-architecture.md`](system-architecture.md) for what the security baseline actually is today.
