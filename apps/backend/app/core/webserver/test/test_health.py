"""Service tests for /api/health.

Hits the framework `/api/health` carve-out through the real ASGI app.

Contract:
- Both pings OK → 200, status="ok"
- DB ping fails → 503, status="degraded"
- Redis ping fails → 503, status="degraded"
- Body always carries {status, db_ok, redis_ok, version}
- version reflects settings.service_version

Ping functions are injected via FastAPI dependency_overrides so no network is
required and the DI pattern (not @patch) is used per project conventions.
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
    monkeypatch.setenv("APP_MODE", "dev")
    # Clear the cached singleton so the monkeypatched env wins.
    # lazy: imported after monkeypatch.setenv so the cache_clear sees fresh state
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    yield
    # Restore: monkeypatch reverts env, but the cache still holds dev settings.
    # Clear it so downstream tests see the conftest-default `APP_MODE=test`.
    get_settings.cache_clear()


@pytest.mark.service
def test_health_both_ok_returns_200() -> None:
    """Both pings healthy → 200 with status='ok'."""
    from app.core.webserver.health import _db_ping, _redis_ping  # noqa: PLC0415
    from app.web import app  # noqa: PLC0415

    async def db_ok() -> bool:
        return True

    async def redis_ok() -> bool:
        return True

    app.dependency_overrides[_db_ping] = db_ok
    app.dependency_overrides[_redis_ping] = redis_ok
    try:
        with TestClient(app) as client:
            r = client.get("/api/health")
    finally:
        app.dependency_overrides.pop(_db_ping, None)
        app.dependency_overrides.pop(_redis_ping, None)

    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"status", "db_ok", "redis_ok", "version"}
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    assert body["redis_ok"] is True
    assert isinstance(body["version"], str)
    assert body["version"]


@pytest.mark.service
def test_health_db_failing_returns_503() -> None:
    """DB ping fails → 503 with status='degraded'; body shape unchanged."""
    from app.core.webserver.health import _db_ping, _redis_ping  # noqa: PLC0415
    from app.web import app  # noqa: PLC0415

    async def db_fail() -> bool:
        return False

    async def redis_ok() -> bool:
        return True

    app.dependency_overrides[_db_ping] = db_fail
    app.dependency_overrides[_redis_ping] = redis_ok
    try:
        with TestClient(app) as client:
            r = client.get("/api/health")
    finally:
        app.dependency_overrides.pop(_db_ping, None)
        app.dependency_overrides.pop(_redis_ping, None)

    assert r.status_code == 503
    body = r.json()
    assert set(body.keys()) == {"status", "db_ok", "redis_ok", "version"}
    assert body["status"] == "degraded"
    assert body["db_ok"] is False
    assert body["redis_ok"] is True


@pytest.mark.service
def test_health_redis_failing_returns_503() -> None:
    """Redis ping fails → 503 with status='degraded'; body shape unchanged."""
    from app.core.webserver.health import _db_ping, _redis_ping  # noqa: PLC0415
    from app.web import app  # noqa: PLC0415

    async def db_ok() -> bool:
        return True

    async def redis_fail() -> bool:
        return False

    app.dependency_overrides[_db_ping] = db_ok
    app.dependency_overrides[_redis_ping] = redis_fail
    try:
        with TestClient(app) as client:
            r = client.get("/api/health")
    finally:
        app.dependency_overrides.pop(_db_ping, None)
        app.dependency_overrides.pop(_redis_ping, None)

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db_ok"] is True
    assert body["redis_ok"] is False


@pytest.mark.service
def test_health_version_reflects_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """version field in response reflects settings.service_version."""
    monkeypatch.setenv("SERVICE_VERSION", "1.2.3-test")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()

    from app.core.webserver.health import _db_ping, _redis_ping  # noqa: PLC0415
    from app.web import app  # noqa: PLC0415

    async def db_ok() -> bool:
        return True

    async def redis_ok() -> bool:
        return True

    app.dependency_overrides[_db_ping] = db_ok
    app.dependency_overrides[_redis_ping] = redis_ok
    try:
        with TestClient(app) as client:
            r = client.get("/api/health")
    finally:
        app.dependency_overrides.pop(_db_ping, None)
        app.dependency_overrides.pop(_redis_ping, None)

    assert r.status_code == 200
    assert r.json()["version"] == "1.2.3-test"
