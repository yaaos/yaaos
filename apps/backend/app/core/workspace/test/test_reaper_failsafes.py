"""Reaper + cleanup-failsafe fault-injection tests.

The reaper enforces three terminal guarantees the rest of M05 depends on:

1. Expired workspaces eventually become `destroyed` — provider.destroy is
   retried up to 3 times across reaper sweeps, then status flips to
   `destroy_failed` so operators can investigate.
2. Idle-timeout sweep flips any `active` workspace past `max_idle_seconds`
   without a claim to `expired`, feeding the destroy pass.
3. Missing provider → `destroy_failed` with an explicit error message; no
   silent stall.

These tests fault-inject each path. They're the missing audit slice flagged
in COMPLETENESS_AUDIT.md (cleanup failsafes).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.plugin_meta import PluginMeta
from app.core.workspace import (
    _reset_providers_for_tests,
    register_workspace_provider,
)
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.service import (
    _attempt_destroy,
    _reaper_sweep_once,
    close_workspace,
    startup_recovery,
)
from app.core.workspace.types import WorkspaceStatus


class _RaisingProvider:
    """WorkspaceProvider whose `destroy` always raises. Counts call attempts
    so tests can assert retry behavior."""

    meta = PluginMeta(id="raises", type="workspace", display_name="raises-on-destroy")

    def __init__(self, error: str = "boom") -> None:
        self.calls = 0
        self.error = error

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"sha": spec.sha}

    async def destroy(self, plugin_state):  # type: ignore[no-untyped-def]
        del plugin_state
        self.calls += 1
        raise RuntimeError(self.error)

    async def health_check(self, plugin_state):  # type: ignore[no-untyped-def]
        del plugin_state
        return None

    async def run_coding_agent_cli(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


class _GoodProvider:
    """Happy-path provider; destroy returns cleanly."""

    meta = PluginMeta(id="good", type="workspace", display_name="good")

    def __init__(self) -> None:
        self.destroy_calls = 0

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"sha": spec.sha}

    async def destroy(self, plugin_state):  # type: ignore[no-untyped-def]
        del plugin_state
        self.destroy_calls += 1

    async def health_check(self, plugin_state):  # type: ignore[no-untyped-def]
        del plugin_state
        return None

    async def run_coding_agent_cli(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture(autouse=True)
def _reset_providers():
    _reset_providers_for_tests()
    yield
    _reset_providers_for_tests()


def _make_row(
    *,
    status: WorkspaceStatus = WorkspaceStatus.EXPIRED,
    provider_id: str = "raises",
    destroy_attempts: int = 0,
    plugin_state: dict | None = None,
    activated_at: datetime | None = None,
    max_idle_seconds: int = 600,
    current_command_id=None,
) -> WorkspaceRow:
    return WorkspaceRow(
        id=uuid4(),
        org_id=uuid4(),
        provider_id=provider_id,
        spec={"sha": "deadbeef"},
        plugin_state=plugin_state or {"working_dir": "/tmp/x"},
        status=status.value,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        destroy_attempts=destroy_attempts,
        activated_at=activated_at,
        max_idle_seconds=max_idle_seconds,
        current_command_id=current_command_id,
    )


# ── _attempt_destroy ────────────────────────────────────────────────────


async def test_destroy_provider_not_registered_marks_destroy_failed(db_session) -> None:  # type: ignore[no-untyped-def]
    """Workspace whose provider_id isn't in the registry must not stall in
    EXPIRED forever — it flips to DESTROY_FAILED with an explicit error."""
    row = _make_row(provider_id="missing_provider")
    db_session.add(row)
    await db_session.commit()

    await _attempt_destroy(row)

    refreshed = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == row.id))).scalar_one()
    assert refreshed.status == WorkspaceStatus.DESTROY_FAILED.value
    assert "missing_provider" in (refreshed.last_destroy_error or "")
    assert "not registered" in (refreshed.last_destroy_error or "")


async def test_destroy_raises_first_attempt_returns_to_expired(db_session) -> None:  # type: ignore[no-untyped-def]
    """A first-attempt failure leaves the row EXPIRED so the next reaper
    sweep picks it up. `destroy_attempts` increments; `last_destroy_error`
    captures the message."""
    provider = _RaisingProvider(error="provider unreachable")
    register_workspace_provider(provider)
    row = _make_row(provider_id="raises", destroy_attempts=0)
    db_session.add(row)
    await db_session.commit()

    await _attempt_destroy(row)

    refreshed = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == row.id))).scalar_one()
    assert refreshed.status == WorkspaceStatus.EXPIRED.value
    assert refreshed.destroy_attempts == 1
    assert "provider unreachable" in (refreshed.last_destroy_error or "")
    assert provider.calls == 1


async def test_destroy_raises_third_attempt_marks_destroy_failed(db_session) -> None:  # type: ignore[no-untyped-def]
    """After the third consecutive failure, status becomes DESTROY_FAILED —
    operator-visible terminal state. No infinite retry."""
    provider = _RaisingProvider()
    register_workspace_provider(provider)
    row = _make_row(provider_id="raises", destroy_attempts=2)
    db_session.add(row)
    await db_session.commit()

    await _attempt_destroy(row)

    refreshed = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == row.id))).scalar_one()
    assert refreshed.status == WorkspaceStatus.DESTROY_FAILED.value
    assert refreshed.destroy_attempts == 3


async def test_destroy_happy_path_marks_destroyed_and_clears_error(db_session) -> None:  # type: ignore[no-untyped-def]
    """A successful destroy clears `last_destroy_error` even when a prior
    attempt set it — important so DESTROYED rows don't carry a stale error."""
    provider = _GoodProvider()
    register_workspace_provider(provider)
    row = _make_row(provider_id="good", destroy_attempts=1)
    row.last_destroy_error = "previous failure"
    db_session.add(row)
    await db_session.commit()

    await _attempt_destroy(row)

    refreshed = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == row.id))).scalar_one()
    assert refreshed.status == WorkspaceStatus.DESTROYED.value
    assert refreshed.destroyed_at is not None
    assert refreshed.last_destroy_error is None
    assert provider.destroy_calls == 1


# ── idle-timeout sweep ────────────────────────────────────────────────────


async def test_idle_sweep_expires_active_workspace_past_max_idle(db_session) -> None:  # type: ignore[no-untyped-def]
    """An ACTIVE workspace with no current claim, activated past its
    `max_idle_seconds`, gets flipped to EXPIRED in the idle-timeout sweep —
    so abandoned workspaces don't linger eating quota."""
    register_workspace_provider(_GoodProvider())
    row = _make_row(
        status=WorkspaceStatus.ACTIVE,
        provider_id="good",
        activated_at=datetime.now(UTC) - timedelta(seconds=900),
        max_idle_seconds=600,
    )
    db_session.add(row)
    await db_session.commit()

    await _reaper_sweep_once()

    refreshed = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == row.id))).scalar_one()
    # Sweep flipped it past EXPIRED → DESTROYED in the same pass (rows-to-destroy
    # query picks up EXPIRED rows after the idle sweep flips them). Either
    # terminal state proves the idle-timeout failsafe fired.
    assert refreshed.status in (
        WorkspaceStatus.EXPIRED.value,
        WorkspaceStatus.DESTROYED.value,
    )


async def test_idle_sweep_leaves_active_workspace_with_live_claim(db_session) -> None:  # type: ignore[no-untyped-def]
    """ACTIVE rows with a live `current_command_id` are skipped — the engine
    owns them. The idle sweep must never yank a workspace out from under an
    in-flight workflow."""
    register_workspace_provider(_GoodProvider())
    row = _make_row(
        status=WorkspaceStatus.ACTIVE,
        provider_id="good",
        activated_at=datetime.now(UTC) - timedelta(seconds=900),
        max_idle_seconds=600,
        current_command_id=uuid4(),
    )
    db_session.add(row)
    await db_session.commit()

    await _reaper_sweep_once()

    refreshed = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == row.id))).scalar_one()
    assert refreshed.status == WorkspaceStatus.ACTIVE.value


async def test_idle_sweep_leaves_recently_activated_workspace(db_session) -> None:  # type: ignore[no-untyped-def]
    """ACTIVE rows still within their idle window are untouched."""
    register_workspace_provider(_GoodProvider())
    row = _make_row(
        status=WorkspaceStatus.ACTIVE,
        provider_id="good",
        activated_at=datetime.now(UTC) - timedelta(seconds=60),
        max_idle_seconds=600,
    )
    db_session.add(row)
    await db_session.commit()

    await _reaper_sweep_once()

    refreshed = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == row.id))).scalar_one()
    assert refreshed.status == WorkspaceStatus.ACTIVE.value


# ── close_workspace idempotency ────────────────────────────────────────


async def test_close_workspace_idempotent_on_already_expired_row(db_session) -> None:  # type: ignore[no-untyped-def]
    """close_workspace's update is filtered to ACTIVE/CREATING — re-calling
    on an already-EXPIRED row is a no-op (status unchanged, no error). Important
    for the CleanupWorkspace-runs-twice case after Tier-2 step retry."""
    row = _make_row(status=WorkspaceStatus.EXPIRED, provider_id="good")
    db_session.add(row)
    await db_session.commit()

    await close_workspace(row.id)

    refreshed = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == row.id))).scalar_one()
    assert refreshed.status == WorkspaceStatus.EXPIRED.value


async def test_close_workspace_no_row_is_silent(db_session) -> None:  # type: ignore[no-untyped-def]
    """A phantom workspace_id (already destroyed and pruned, or never
    existed) doesn't raise — close_workspace is fully idempotent."""
    _ = db_session
    await close_workspace(uuid4())  # must not raise


# ── startup_recovery (orphan-row recovery from prior process crash) ─────


async def test_startup_recovery_flips_orphan_rows_to_expired(db_session) -> None:  # type: ignore[no-untyped-def]
    """A prior process crashed mid-workflow leaving rows in non-terminal
    states (CREATING / ACTIVE / DESTROYING). `startup_recovery()` runs at
    FastAPI lifespan start and flips them all to EXPIRED so the reaper
    picks them up — no stuck workspaces survive a restart. This is the
    Python side of failsafe #4 (startup reconciliation)."""
    rows = [
        _make_row(status=WorkspaceStatus.CREATING, provider_id="good"),
        _make_row(status=WorkspaceStatus.ACTIVE, provider_id="good"),
        _make_row(status=WorkspaceStatus.DESTROYING, provider_id="good"),
        # Already-terminal rows must NOT be re-flipped.
        _make_row(status=WorkspaceStatus.DESTROYED, provider_id="good"),
        _make_row(status=WorkspaceStatus.DESTROY_FAILED, provider_id="good"),
    ]
    for row in rows:
        db_session.add(row)
    await db_session.commit()

    await startup_recovery()

    refreshed = (await db_session.execute(select(WorkspaceRow).order_by(WorkspaceRow.id))).scalars().all()
    by_id = {r.id: r for r in refreshed}
    # Three orphans flipped.
    assert by_id[rows[0].id].status == WorkspaceStatus.EXPIRED.value
    assert by_id[rows[1].id].status == WorkspaceStatus.EXPIRED.value
    assert by_id[rows[2].id].status == WorkspaceStatus.EXPIRED.value
    # Terminal rows untouched.
    assert by_id[rows[3].id].status == WorkspaceStatus.DESTROYED.value
    assert by_id[rows[4].id].status == WorkspaceStatus.DESTROY_FAILED.value
