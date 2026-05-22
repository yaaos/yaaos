"""Top-level pytest fixtures shared across all backend module tests."""

from __future__ import annotations

import os
import warnings
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Set test env vars BEFORE any app imports so module-level `get_settings()` works.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yaaos:yaaos@localhost:5432/yaaos_test")
os.environ.setdefault("YAAOS_ENCRYPTION_KEY", "vrGOcrqpNIMof1qsuwOEVYvgxo-03dCX8lfVXm_G4JI=")
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
os.environ.setdefault("YAAOS_CATCHUP_DELAY_SECONDS", "0")


@pytest.fixture(scope="session", autouse=True)
def _quiet_pydantic_warnings() -> None:
    """Suppress noisy pydantic deprecation warnings during tests."""
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="pydantic.*")


@pytest.fixture(autouse=True)
def _ensure_plugin_registries_populated(request):
    """Defensively re-bootstrap plugin registries before each service test.

    Some unit tests (e.g. `test_coding_agent/test_registry.py`) call
    `_reset_plugins_for_tests()` in teardown, which clears the global
    registries and leaks empty state to subsequent tests. Service tests that
    drive `reviewer.start_pr_review` / `intake.handle_vcs_events` need the
    real plugins registered (then wrapped by the testing stubs); without
    this fixture, ordering controls whether they pass.

    Idempotent + cheap. Only fires for tests carrying `@pytest.mark.service`
    so the broader unit suite isn't affected.
    """
    if request.node.get_closest_marker("service") is None:
        return
    from app.testing.service_test_setup import ensure_plugins_registered  # noqa: PLC0415

    ensure_plugins_registered()


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

    from app.core.database import get_engine, set_test_session_override  # noqa: PLC0415

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
