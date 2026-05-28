"""Integration test for /api/health.

Hits the framework `/api/health` carve-out through the real ASGI app. The DB
ping returns True when Postgres is reachable, False otherwise — the test
asserts the endpoint returns 200 either way (status text reflects the result).
"""

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure required env vars exist so Settings() doesn't raise on construction."""
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://yaaos:yaaos@localhost:5432/yaaos",
        ),
    )
    monkeypatch.setenv(
        "YAAOS_ENCRYPTION_KEY",
        os.environ.get(
            "YAAOS_ENCRYPTION_KEY",
            # Test-only Fernet key — generated for tests, not used in any other context.
            "VHJ5SW5nTm90VG9CcmVha1lvdXJTZWNyZXRzS2V5MTIzPQ==",
        ),
    )
    monkeypatch.setenv("YAAOS_ENV", "dev")
    # Clear the cached singleton so the monkeypatched env wins.
    # lazy: imported after monkeypatch.setenv so the cache_clear sees fresh state
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    yield
    # Restore: monkeypatch reverts env, but the cache still holds dev settings.
    # Clear it so downstream tests see the conftest-default `YAAOS_ENV=test`.
    get_settings.cache_clear()


def test_health_endpoint_responds_200() -> None:
    # lazy: import after the fixture has set env vars
    from app.web import app  # noqa: PLC0415

    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"status", "db_ok", "redis_ok", "version"}
    assert body["status"] in {"ok", "degraded"}
    assert isinstance(body["db_ok"], bool)
    assert isinstance(body["redis_ok"], bool)
    assert body["version"]


def test_health_endpoint_status_matches_db_and_redis() -> None:
    # lazy: import after the fixture has set env vars
    from app.web import app  # noqa: PLC0415

    with TestClient(app) as client:
        r = client.get("/api/health")
    body = r.json()
    if body["db_ok"] and body["redis_ok"]:
        assert body["status"] == "ok"
    else:
        assert body["status"] == "degraded"
