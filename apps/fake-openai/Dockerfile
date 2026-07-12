# Minimal fake-openai image for the test stack.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY apps/fake-openai/pyproject.toml ./pyproject.toml
COPY apps/fake-openai/app ./app

ENV UV_LINK_MODE=copy

RUN uv sync --no-dev || uv sync

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
