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
            "postgresql+asyncpg://yaaof:yaaof@localhost:5432/yaaof",
        ),
    )
    monkeypatch.setenv(
        "YAAOF_ENCRYPTION_KEY",
        os.environ.get(
            "YAAOF_ENCRYPTION_KEY",
            # Test-only Fernet key — generated for tests, not used in any other context.
            "VHJ5SW5nTm90VG9CcmVha1lvdXJTZWNyZXRzS2V5MTIzPQ==",
        ),
    )
    monkeypatch.setenv("YAAOF_ENV", "dev")
    # Clear the cached singleton so the monkeypatched env wins.
    # lazy: imported after monkeypatch.setenv so the cache_clear sees fresh state
    from app.core.config.service import get_settings  # noqa: PLC0415

    get_settings.cache_clear()


def test_health_endpoint_responds_200() -> None:
    # lazy: import after the fixture has set env vars
    from app.main import app  # noqa: PLC0415

    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"status", "db_ok", "version"}
    assert body["status"] in {"ok", "degraded"}
    assert isinstance(body["db_ok"], bool)
    assert body["version"]


def test_health_endpoint_status_matches_db_ok() -> None:
    # lazy: import after the fixture has set env vars
    from app.main import app  # noqa: PLC0415

    with TestClient(app) as client:
        r = client.get("/api/health")
    body = r.json()
    if body["db_ok"]:
        assert body["status"] == "ok"
    else:
        assert body["status"] == "degraded"
