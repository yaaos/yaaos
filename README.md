# yaaof

Self-hosted, team-scale agent orchestration service. See [`plan/VISION.md`](plan/VISION.md) for the long-horizon view; [`plan/ROADMAP.md`](plan/ROADMAP.md) for the active milestone.

> **Status:** skeleton. The walking-skeleton scaffolds the foundation; M01 features (review agents, tickets, GitHub plugin, Claude Code plugin) are not yet implemented.

## Quick start (Docker)

```bash
# Generate a Fernet key once and persist it.
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Copy .env.sample → .env and paste the key into YAAOF_ENCRYPTION_KEY.
cp .env.sample .env
# (edit .env)

# Build + run.
docker compose -f docker/docker-compose.yml --env-file .env up --build
```

Visit [http://localhost:8080](http://localhost:8080) — the page renders "Hello World" plus the live `/api/health` response.

## Dev workflow (without Docker)

```bash
# Backend (Python 3.13).
cd apps/backend
uv sync
uv run uvicorn app.main:app --port 8080 --reload

# Frontend (Node 22 + pnpm). Separate terminal.
cd apps/web
pnpm install
pnpm dev   # http://localhost:5173, proxies /api/* to :8080
```

You'll also need Postgres 16 running locally (matching `DATABASE_URL` in `.env`).

## CI

```bash
apps/backend/bin/ci   # ruff + tach + check_table_access + check_patch_usage + pytest
apps/web/bin/ci       # biome + tsc + vitest + vite build
apps/e2e/bin/ci       # brings up docker-compose.test.yml, runs Playwright, tears down. Skips until that stack exists.
```

## Repo layout

- `apps/` — backend (FastAPI), web (React SPA), e2e (Playwright).
- `bin/` — repo-wide tools: `sync_modules`, `check_table_access`, `check_patch_usage`.
- `docker/` — `Dockerfile`, `docker-compose.yml`.
- `docs/` — present-tense docs for shipped modules.
- `plan/` — future-tense planning (vision, roadmap, M01 milestone).

## Working with the assistant

See [`CLAUDE.md`](CLAUDE.md) for conventions.
