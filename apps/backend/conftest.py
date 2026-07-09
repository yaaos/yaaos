"""Top-level pytest fixtures shared across all backend module tests."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import warnings
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

# Runtime import audit — install BEFORE any app.* import so the meta-path
# finder observes every backend module load. Defense-in-depth on top of
# bin/sync_modules' static checks: catches dynamic-Python reaches
# (importlib.import_module, __import__, getattr-triggered lazy loads,
# string-built plugin dispatch) that no AST analysis can see. See
# bin/import_audit.py for the rule definition.
_BIN_DIR = Path(__file__).resolve().parent / "bin"
if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))
import import_audit  # noqa: E402

import_audit.install()

# Set test env vars BEFORE any app imports so module-level `get_settings()` works.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yaaos:yaaos@localhost:5432/yaaos_test")
os.environ.setdefault("YAAOS_ENCRYPTION_KEY", "vrGOcrqpNIMof1qsuwOEVYvgxo-03dCX8lfVXm_G4JI=")
# Required by core/config; only tests that publish/subscribe actually
# connect (lazy client). Tests that need a live Redis check reachability
# themselves and skip if absent.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
# APP_MODE must be `test` for the suite — `get_engine()` switches to NullPool
# whenever `is_non_prod` (dev or test), and the `oauth_test` plugin asserts on
# this exact value to refuse loading outside the test env. The `backend-ci`
# Docker stage inherits `APP_MODE=production` from the prod base; force the
# override here so the test invocation environment doesn't leak prod semantics.
os.environ["APP_MODE"] = "test"
# ENVIRONMENT has no default — Settings refuses to construct without it.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("YAAOS_CODING_AGENT_STUB", "1")
os.environ.setdefault("YAAOS_REVIEW_DEBOUNCE_SECONDS", "0")
os.environ.setdefault("YAAOS_REAPER_INTERVAL_SECONDS", "1")
os.environ.setdefault("YAAOS_HEARTBEAT_INTERVAL_SECONDS", "1")
# Required. Full external origin; the derived public_hostname (its netloc) is
# the agent identity-exchange audience the tests sign.
os.environ.setdefault("YAAOS_PUBLIC_ORIGIN", "https://app.yaaos.dev")

# Re-export autouse isolation fixtures so pytest auto-discovers them. The import
# is deferred until after env vars are set because app.testing.isolation triggers
# app.core.redis → app.core.config at import time.
from app.testing.isolation import (  # noqa: E402, F401
    actions_registry_isolation,
    bearer_verify_isolation,
    email_inbox_isolation,
    intake_registry_isolation,
    plugin_registries_isolation,
    pubsub_isolation,
    scheduler_registry_isolation,
    sse_shutdown_event_isolation,
    sts_verify_isolation,
    subscriber_registry_isolation,
    workspace_providers_isolation,
)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Flush the runtime import-audit report and fail the suite on any violation.

    Writes `tmp/import_audit_violations.json` when non-empty; overrides
    pytest's exit status to 2 so `bin/ci`'s `set -e` halts. The sentinel file
    `tmp/import_audit_ran` (written at install) lets `bin/ci` independently
    prove the guard ran even when the run is otherwise green.
    """
    count = import_audit.flush_and_report()
    if count and session.exitstatus == 0:
        session.exitstatus = 2


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

    from app.core.database import get_engine, set_db_session_for_tests  # noqa: PLC0415

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

        with set_db_session_for_tests(async_session):
            try:
                yield async_session
            finally:
                await async_session.close()
                if outer_trans.is_active:
                    await outer_trans.rollback()


def _fake_github_dir() -> Path:
    p = Path(__file__).resolve()
    for parent in p.parents:
        if parent.name == "backend":
            return parent.parent / "fake-github"
    raise RuntimeError("could not locate the apps/backend ancestor of this test file")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_github_base_url(tmp_path: Path) -> Iterator[str]:
    """Spawn a live `apps/fake-github` uvicorn subprocess on an ephemeral port,
    backed by a fresh temp dir for its bare git repos. Yields the base URL;
    terminates the subprocess on teardown.

    Top-level (not module-local) so any module's tests can round-trip
    against a live fake-github subprocess rather than `httpx_mock` — real
    HTTP round trips, real git smart-HTTP protocol for push, matching how
    the e2e stack runs it.

    Skips (rather than fails) when `apps/fake-github/.venv` hasn't been
    created yet — `cd apps/fake-github && uv sync` provisions it; `bin/ci`
    does this as part of its own setup surface for this fixture.
    """
    fake_github_dir = _fake_github_dir()
    venv_python = fake_github_dir / ".venv" / "bin" / "python"
    if not venv_python.exists():
        pytest.skip(f"{venv_python} missing — run `uv sync` in apps/fake-github first")

    port = _free_port()
    repos_dir = tmp_path / "fake-github-repos"
    repos_dir.mkdir()
    env = {
        **os.environ,
        "FAKE_GITHUB_REPOS_DIR": str(repos_dir),
        "GITHUB_WEBHOOK_SECRET": "TEST-FAKE-NOT-FOR-PROD-aaaaaaaaaaaaaaaa",
    }
    proc = subprocess.Popen(
        [str(venv_python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(fake_github_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        healthy = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read().decode() if proc.stdout else ""
                raise RuntimeError(f"fake-github exited early:\n{out}")
            try:
                resp = httpx.get(f"{base_url}/__test/posted_comments", timeout=0.5)
                if resp.status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        if not healthy:
            raise RuntimeError("fake-github did not become healthy in time")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
