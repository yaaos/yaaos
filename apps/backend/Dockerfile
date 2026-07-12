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

# VITE_* build args — inlined into the SPA bundle at compile time.
# Dash0 observability values (endpoint, auth token, dataset) are injected
# by the RWX deploy-prod task from the yaaos vault.  VITE_SERVICE_VERSION
# comes from the CI git SHA; VITE_ENVIRONMENT is the deployment tier tag
# (independent of Vite's own --mode, which controls optimization level only).
ARG VITE_OTEL_COLLECTOR_ENDPOINT
ARG VITE_DASH0_AUTH_TOKEN
ARG VITE_DASH0_DATASET
ARG VITE_SERVICE_VERSION
ARG VITE_ENVIRONMENT

ENV VITE_OTEL_COLLECTOR_ENDPOINT=${VITE_OTEL_COLLECTOR_ENDPOINT}
ENV VITE_DASH0_AUTH_TOKEN=${VITE_DASH0_AUTH_TOKEN}
ENV VITE_DASH0_DATASET=${VITE_DASH0_DATASET}
ENV VITE_SERVICE_VERSION=${VITE_SERVICE_VERSION}
ENV VITE_ENVIRONMENT=${VITE_ENVIRONMENT}

# Source + build
COPY apps/web ./apps/web

RUN pnpm --filter ./apps/web build --mode production

# ────────────────────────────────────────────────────────────────────────
# Stage 2: backend runtime image
# ────────────────────────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm AS backend

# Runtime tools:
#   - tini: PID-1 init — forwards SIGTERM and reaps orphaned children.
#   - ca-certificates: TLS for outbound calls (GitHub, Neon, Upstash, SMTP, …).
#   - libxmlsec1 + libxml2/libxslt + pkg-config: build/runtime deps for
#     python-xmlsec (SAML).
#
# NOT installed: Node.js, the Claude Code CLI, and git. These supported the old
# in-process review model (plugins/claude_code shelled out to `claude`; the
# in-process workspace ran `git clone`). Reviews now run on the remote
# WorkspaceAgent (apps/agent), which carries the CLI and does the clone; the
# control plane makes zero direct LLM calls. NOTE: the in-process code path
# (apps/backend/app/plugins/claude_code/service.py) is still live and is being
# removed on a separate branch — until that lands, in-process reviews fail in
# this image. Accepted intentionally.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates tini \
       libxmlsec1-dev xmlsec1 pkg-config libxml2-dev libxslt1-dev \
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

# Skill and agent sources for the per-vendor skills-bundle endpoint.
# The backend reads these at request time via `build_skills_bundle_zip`;
# the path is configured by YAAOS_SKILLS_SOURCE_DIR (consumed by Settings).
COPY .claude/skills /app/yaaos_skills/skills
COPY .claude/agents /app/yaaos_skills/agents
ENV YAAOS_SKILLS_SOURCE_DIR=/app/yaaos_skills

# Built SPA from stage 1
COPY --from=web-builder /build/apps/web/dist ./apps/web/dist

# Install the backend project itself
RUN uv sync --frozen --package yaaos-backend 2>/dev/null || uv sync --package yaaos-backend

# Run as a non-root user — standard least-privilege hardening for the
# production web/worker processes.
RUN useradd --create-home --uid 1000 --shell /bin/bash yaaos \
 && chown -R yaaos:yaaos /app
USER yaaos

# Runtime
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    APP_MODE=production

EXPOSE 8080

# tini as PID 1: forwards SIGTERM to the Python child and reaps any orphaned
# sub-processes. Both role stages inherit this.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default CMD lives on the per-role stages below (`web`, `worker`).
# Building the bare `backend` target should never be done in production;
# always target `web` or `worker`.

# ────────────────────────────────────────────────────────────────────────
# Stage 3a: api — FastAPI/uvicorn HTTP + SSE process
# ────────────────────────────────────────────────────────────────────────
FROM backend AS web

CMD ["python", "apps/backend/app/web.py"]

# ────────────────────────────────────────────────────────────────────────
# Stage 3b: worker — taskiq consumer + outbox drain process
# Same build as `api`; only CMD differs. Run as a separate container so
# uvicorn worker count and taskiq concurrency can scale independently.
# ────────────────────────────────────────────────────────────────────────
FROM backend AS worker

CMD ["python", "apps/backend/app/worker.py"]
