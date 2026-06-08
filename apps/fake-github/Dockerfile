# Minimal fake-github image for the test stack.
FROM python:3.14-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY apps/fake-github/pyproject.toml ./pyproject.toml
COPY apps/fake-github/app ./app

ENV UV_LINK_MODE=copy

# git is required by the git HTTP backend (git http-backend + git init/clone)
# for serving ProvisionWorkspace clone requests from the agent container.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

RUN uv sync --no-dev || uv sync

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
