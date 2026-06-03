"""Top-level pytest fixtures shared across all backend module tests."""

from __future__ import annotations

import asyncio
import os
import warnings
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Set test env vars BEFORE any app imports so module-level `get_settings()` works.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yaaos:yaaos@localhost:5432/yaaos_test")
os.environ.setdefault("YAAOS_ENCRYPTION_KEY", "vrGOcrqpNIMof1qsuwOEVYvgxo-03dCX8lfVXm_G4JI=")
# Required by core/config; only tests that publish/subscribe actually
# connect (lazy client). Tests that need a live Redis check reachability
# themselves and skip if absent.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
# YAAOS_ENV must be `test` for the suite — `get_engine()` switches to NullPool
# whenever `is_non_prod` (dev or test), and the `oauth_test` plugin asserts on
# this exact value to refuse loading outside the test env. The `backend-ci`
# Docker stage inherits `YAAOS_ENV=prod` from the prod base; force the
# override here so the test invocation environment doesn't leak prod semantics.
os.environ["YAAOS_ENV"] = "test"
os.environ.setdefault("YAAOS_CODING_AGENT_STUB", "1")
os.environ.setdefault("YAAOS_REVIEW_DEBOUNCE_SECONDS", "0")
os.environ.setdefault("YAAOS_REAPER_INTERVAL_SECONDS", "1")
os.environ.setdefault("YAAOS_HEARTBEAT_INTERVAL_SECONDS", "1")
os.environ.setdefault("YAAOS_MCP_TOKEN_SWEEP_INTERVAL_SECONDS", "1")
# Required for agent identity-exchange audience binding.
# Value matches what the identity-exchange tests sign as the audience.
os.environ.setdefault("YAAOS_PUBLIC_HOSTNAME", "app.yaaos.cloud")

# Re-export autouse isolation fixtures so pytest auto-discovers them. The import
# is deferred until after env vars are set because app.testing.isolation triggers
# app.core.redis → app.core.config at import time.
from app.testing.isolation import (  # noqa: F401
    _canonical_registries,
    email_inbox_isolation,
    plugin_registries_isolation,
    pubsub_isolation,
    recovery_policies_isolation,
    subscriber_registry_isolation,
    workflow_context_provider_isolation,
    workspace_providers_isolation,
)


@pytest.fixture(scope="session", autouse=True)
def _quiet_pydantic_warnings() -> None:
    """Suppress noisy pydantic deprecation warnings during tests."""
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="pydantic.*")


@pytest.fixture(scope="session", autouse=True)
def _shutdown_runtime_at_session_end():
    """Run registered shutdown hooks once at session teardown.

    Suppresses "pending task" warnings and exercises the prod shutdown
    path as a smoke test. Runs regardless of test marker.
    """
    yield
    from app.testing.lifecycle import shutdown_runtime  # noqa: PLC0415

    # pytest-asyncio event loop is already torn down here; asyncio.run() creates a fresh loop for cleanup.
    asyncio.run(shutdown_runtime())


@pytest_asyncio.fixture(scope="session")
async def _redis_reachable() -> bool:
    """Probe `settings.redis_url` once per session. Tests that publish or
    subscribe via `core/sse` use `redis_or_skip` (below) to skip
    cleanly when Redis is unavailable — local dev workflows without a
    Redis container aren't blocked."""
    from redis.asyncio import from_url  # noqa: PLC0415
    from redis.exceptions import RedisError  # noqa: PLC0415

    from app.core.config import get_settings  # noqa: PLC0415

    try:
        client = from_url(get_settings().redis_url, decode_responses=True)
        try:
            await client.ping()
            return True
        finally:
            await client.aclose()
    except RedisError, OSError:
        return False


@pytest.fixture
def redis_or_skip(_redis_reachable: bool) -> None:
    """Function-scoped: skip the test if Redis isn't reachable.
    `pytestmark = pytest.mark.usefixtures("redis_or_skip")` at the top of
    a module marks every test in it as Redis-dependent.
    """
    if not _redis_reachable:
        pytest.skip("Redis not reachable at settings.redis_url")


@pytest_asyncio.fixture(scope="session")
async def _migrated_schema() -> AsyncIterator[None]:
    """Run schema migrations once per session against the test DB.

    The test DB is shared across the entire session; each test runs inside a
    transaction that gets rolled back at teardown (see `db_session`), so tests
    don't see each other's writes.
    """
    from app.core.database import migrate  # noqa: PLC0415

    await migrate()
    yield


@pytest_asyncio.fixture
async def db_session(_migrated_schema: None) -> AsyncIterator:
    """Transactional-rollback session for tests that hit Postgres.

    Opens a connection, begins an outer transaction, binds an `AsyncSession`
    to that connection, and installs the session as the global override via
    `set_test_session_override`. Production code's `async with session() as s:`
    calls hit the overridden session — all writes happen inside the outer
    transaction.

    Inner production-side `await s.commit()` calls become SAVEPOINT releases
    (not real commits) via a `restart_savepoint` listener; the outer
    transaction is rolled back on teardown so the DB is clean for the next
    test.

    Tests that don't need DB access don't depend on this fixture; the
    override stays unset and `session()` falls through to the real factory.
    """
    from sqlalchemy import event  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: PLC0415

    from app.core.database import get_engine  # noqa: PLC0415
    from app.core.database.service import set_test_session_override  # noqa: PLC0415

    engine = get_engine()
    async with engine.connect() as connection:
        outer_trans = await connection.begin()
        async_session = AsyncSession(bind=connection, expire_on_commit=False)

        # First SAVEPOINT inside the outer transaction.
        await connection.begin_nested()

        # Production-side `await s.commit()` ends the inner SAVEPOINT; open a
        # fresh one so the next write lands in a new inner scope. The outer
        # transaction remains active and rolls back at teardown.
        @event.listens_for(async_session.sync_session, "after_transaction_end")
        def _restart_savepoint(_sess, trans) -> None:  # type: ignore[no-untyped-def]
            if trans.nested and not trans._parent.nested:
                connection.sync_connection.begin_nested()

        set_test_session_override(async_session)
        try:
            yield async_session
        finally:
            set_test_session_override(None)
            await async_session.close()
            if outer_trans.is_active:
                await outer_trans.rollback()
