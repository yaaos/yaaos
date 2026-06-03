# syntax=docker/dockerfile:1.7

# ────────────────────────────────────────────────────────────────────────
# Stage 1: build the SPA
# ────────────────────────────────────────────────────────────────────────
FROM node:24-bookworm-slim AS web-builder

WORKDIR /build

# Enable pnpm via corepack
RUN corepack enable && corepack prepare pnpm@latest --activate

# Workspace files
COPY package.json pnpm-lock.yaml* pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/

# Install deps for the web workspace only
RUN pnpm install --filter ./apps/web... --frozen-lockfile || pnpm install --filter ./apps/web...

# Source + build
COPY apps/web ./apps/web
RUN pnpm --filter ./apps/web build

# ────────────────────────────────────────────────────────────────────────
# Stage 2: backend runtime image
# ────────────────────────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm AS backend

# Runtime tools:
#   - git: plugins/in_process_workspace runs `git clone` during workspace
#     provision (the reviewer needs the repo checked out for the agent's
#     Read/Glob/Grep tools).
#   - node + the Claude Code CLI: plugins/claude_code shells out to `claude`
#     to do the actual review. The CLI runs in-process here; moving agents into
#     Docker workspaces carries the CLI with them and drops this layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl gnupg tini \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get install -y --no-install-recommends libxmlsec1-dev xmlsec1 pkg-config libxml2-dev libxslt1-dev \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

# uv for fast Python installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Backend project files (uv workspace root + member)
COPY pyproject.toml uv.lock* ./
COPY apps/backend/pyproject.toml apps/backend/

# Install backend deps into /app/.venv
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
RUN uv sync --frozen --package yaaos-backend --no-install-project 2>/dev/null \
    || uv sync --package yaaos-backend --no-install-project

# Backend source
COPY apps/backend ./apps/backend

# Built SPA from stage 1
COPY --from=web-builder /build/apps/web/dist ./apps/web/dist

# Install the backend project itself
RUN uv sync --frozen --package yaaos-backend 2>/dev/null || uv sync --package yaaos-backend

# Drop root: the Claude Code CLI refuses --dangerously-skip-permissions when
# uid is 0, and the reviewer needs that flag to run unattended.
RUN useradd --create-home --uid 1000 --shell /bin/bash yaaos \
 && chown -R yaaos:yaaos /app
USER yaaos

# Runtime
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    YAAOS_ENV=prod

EXPOSE 8080

# tini as PID 1: forwards SIGTERM to the Python child and reaps orphaned
# sub-processes (Claude Code CLI, git clone). Both role stages inherit this.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default CMD lives on the per-role stages below (`web`, `worker`).
# Building the bare `backend` target should never be done in production;
# always target `web` or `worker`.

# ────────────────────────────────────────────────────────────────────────
# Stage 3a: yaaos-web — FastAPI/uvicorn HTTP + SSE process
# ────────────────────────────────────────────────────────────────────────
FROM backend AS web

CMD ["python", "apps/backend/app/web.py"]

# ────────────────────────────────────────────────────────────────────────
# Stage 3b: yaaos-worker — taskiq consumer + outbox drain process
# Same build as `web`; only CMD differs. Run as a separate container so
# uvicorn worker count and taskiq concurrency can scale independently.
# ────────────────────────────────────────────────────────────────────────
FROM backend AS worker

CMD ["python", "apps/backend/app/worker.py"]
