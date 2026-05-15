# syntax=docker/dockerfile:1.7

# ────────────────────────────────────────────────────────────────────────
# Stage 1: build the SPA
# ────────────────────────────────────────────────────────────────────────
FROM node:22-bookworm-slim AS web-builder

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
FROM python:3.13-slim-bookworm AS backend

# uv for fast Python installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Backend project files (uv workspace root + member)
COPY pyproject.toml uv.lock* ./
COPY apps/backend/pyproject.toml apps/backend/

# Install backend deps into /app/.venv
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
RUN uv sync --frozen --package yaaof-backend --no-install-project 2>/dev/null \
    || uv sync --package yaaof-backend --no-install-project

# Backend source
COPY apps/backend ./apps/backend

# Built SPA from stage 1
COPY --from=web-builder /build/apps/web/dist ./apps/web/dist

# Install the backend project itself
RUN uv sync --frozen --package yaaof-backend 2>/dev/null || uv sync --package yaaof-backend

# Runtime
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    YAAOF_ENV=prod

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--app-dir", "apps/backend"]
