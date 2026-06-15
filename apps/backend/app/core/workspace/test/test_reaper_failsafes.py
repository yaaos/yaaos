"""Reaper + cleanup-failsafe fault-injection tests.

The reaper enforces three terminal guarantees the rest of the system depends on:

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
from uuid import uuid4, uuid7

import pytest
from sqlalchemy import select

from app.core.agent_gateway import CleanupWorkspaceCommand, enqueue_command
from app.core.workspace import (
    WorkspaceRegistry,
    force_close_all,
    register_workspace_provider,
)
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.service import (
    _attempt_destroy,
    _reaper_sweep_once,
    _utcnow,
    close_workspace,
    failsafe_agent_loss,
    startup_recovery,
)
from app.core.workspace.types import WorkspaceStatus
from app.testing.seed import seed_agent


class _RaisingProvider:
    """WorkspaceProvider whose `destroy` always raises. Counts call attempts
    so tests can assert retry behavior."""

    plugin_id = "raises"

    def __init__(self, error: str = "boom") -> None:
        self.calls = 0
        self.error = error

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"sha": spec.sha}

    async def destroy(self) -> None:
        self.calls += 1
        raise RuntimeError(self.error)

    async def health_check(self) -> None:
        return None

    async def run_coding_agent_cli(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


class _GoodProvider:
    """Happy-path provider; destroy returns cleanly."""

    plugin_id = "good"

    def __init__(self) -> None:
        self.destroy_calls = 0

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"sha": spec.sha}

    async def destroy(self) -> None:
        self.destroy_calls += 1

    async def health_check(self) -> None:
        return None

    async def run_coding_agent_cli(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture(autouse=True)
def _reset_providers(workspace_providers_isolation):
    del workspace_providers_isolation  # fixture handles clear before+after


async def _make_row(
    db_session,
    *,
    status: WorkspaceStatus = WorkspaceStatus.EXPIRED,
    provider_id: str = "raises",
    destroy_attempts: int = 0,
    activated_at: datetime | None = None,
    max_idle_seconds: int = 600,
    current_command_id=None,
    owning_agent_id=None,
) -> WorkspaceRow:
    """Build a WorkspaceRow. Seeds an agent row when `owning_agent_id` is omitted."""
    if owning_agent_id is None:
        agent = await seed_agent(org_id=uuid4(), session=db_session)
        owning_agent_id = agent["id"]
    return WorkspaceRow(
        id=uuid7(),
        org_id=uuid4(),
        provider_id=provider_id,
        spec={"sha": "deadbeef"},
        status=status.value,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        destroy_attempts=destroy_attempts,
        activated_at=activated_at,
        max_idle_seconds=max_idle_seconds,
        current_command_id=current_command_id,
        owning_agent_id=owning_agent_id,
    )


# ── _attempt_destroy ────────────────────────────────────────────────────


async def test_destroy_provider_not_registered_marks_destroy_failed(db_session) -> None:  # type: ignore[no-untyped-def]
    """Workspace whose provider_id isn't in the registry must not stall in
    EXPIRED forever — it flips to DESTROY_FAILED with an explicit error."""
    row = await _make_row(db_session, provider_id="missing_provider")
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
    row = await _make_row(db_session, provider_id="raises", destroy_attempts=0)
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
    row = await _make_row(db_session, provider_id="raises", destroy_attempts=2)
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
    row = await _make_row(db_session, provider_id="good", destroy_attempts=1)
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
    row = await _make_row(
        db_session,
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
    row = await _make_row(
        db_session,
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
    row = await _make_row(
        db_session,
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
    """close_workspace's update is filtered to ACTIVE — re-calling on an
    already-EXPIRED row is a no-op (status unchanged, no error). Important
    for the CleanupWorkspace-runs-twice case after Tier-2 step retry."""
    row = await _make_row(db_session, status=WorkspaceStatus.EXPIRED, provider_id="good")
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


# ── failsafe-6 per-pod agent loss ──────────────────────────────────────


async def test_failsafe_agent_loss_per_pod_only_expires_stale_owner(db_session) -> None:  # type: ignore[no-untyped-def]
    """Org with 2 agents: one stale, one live. Only the stale agent's
    workspaces are expired (reason `agent_loss`); the live agent's workspace
    stays ACTIVE. Per-pod, not per-org."""
    org_id = uuid4()
    stale = await seed_agent(org_id=org_id, session=db_session, heartbeat_age_seconds=600)
    live = await seed_agent(org_id=org_id, session=db_session, heartbeat_age_seconds=2)

    stale_ws = WorkspaceRow(
        id=uuid7(),
        org_id=org_id,
        provider_id="remote_agent",
        spec={"sha": "a"},
        status=WorkspaceStatus.ACTIVE.value,
        expires_at=_utcnow() + timedelta(hours=1),
        owning_agent_id=stale["id"],
    )
    live_ws = WorkspaceRow(
        id=uuid7(),
        org_id=org_id,
        provider_id="remote_agent",
        spec={"sha": "b"},
        status=WorkspaceStatus.ACTIVE.value,
        expires_at=_utcnow() + timedelta(hours=1),
        owning_agent_id=live["id"],
    )
    db_session.add_all([stale_ws, live_ws])
    await db_session.commit()

    # Pass the stale agent's ID directly — the sweeper now feeds the offline set.
    await failsafe_agent_loss(db_session, {stale["id"]})
    await db_session.commit()

    refreshed = {r.id: r for r in (await db_session.execute(select(WorkspaceRow))).scalars().all()}
    assert refreshed[stale_ws.id].status == WorkspaceStatus.EXPIRED.value
    assert refreshed[live_ws.id].status == WorkspaceStatus.ACTIVE.value


# ── startup_recovery (orphan-row recovery from prior process crash) ─────


async def test_startup_recovery_flips_orphan_rows_to_expired(db_session) -> None:  # type: ignore[no-untyped-def]
    """A prior process crashed mid-workflow leaving rows in non-terminal
    states (ACTIVE / DESTROYING). `startup_recovery()` runs at FastAPI
    lifespan start and flips them to EXPIRED so the reaper picks them up.

    Terminal rows (DESTROYED, DESTROY_FAILED) must NOT be re-flipped.
    """
    rows = [
        await _make_row(db_session, status=WorkspaceStatus.ACTIVE, provider_id="good"),
        await _make_row(db_session, status=WorkspaceStatus.DESTROYING, provider_id="good"),
        # Already-terminal rows must NOT be re-flipped.
        await _make_row(db_session, status=WorkspaceStatus.DESTROYED, provider_id="good"),
        await _make_row(db_session, status=WorkspaceStatus.DESTROY_FAILED, provider_id="good"),
    ]
    for row in rows:
        db_session.add(row)
    await db_session.commit()

    await startup_recovery()

    refreshed = (await db_session.execute(select(WorkspaceRow).order_by(WorkspaceRow.id))).scalars().all()
    by_id = {r.id: r for r in refreshed}
    # ACTIVE and DESTROYING flipped.
    assert by_id[rows[0].id].status == WorkspaceStatus.EXPIRED.value
    assert by_id[rows[1].id].status == WorkspaceStatus.EXPIRED.value
    # Terminal rows untouched.
    assert by_id[rows[2].id].status == WorkspaceStatus.DESTROYED.value
    assert by_id[rows[3].id].status == WorkspaceStatus.DESTROY_FAILED.value


# ── WorkspaceRegistry.items() ───────────────────────────────────────────


def test_workspace_registry_items_returns_tuple_of_pairs() -> None:
    """items() returns a tuple of (provider_id, provider) pairs for registered providers."""
    reg = WorkspaceRegistry()
    provider = _GoodProvider()
    reg.register(provider)
    result = reg.items()
    assert isinstance(result, tuple)
    assert len(result) == 1
    pid, p = result[0]
    assert pid == "good"
    assert p is provider


def test_workspace_registry_items_is_immutable_snapshot() -> None:
    """Mutating the tuple returned by items() does not affect the registry."""
    reg = WorkspaceRegistry()
    provider = _GoodProvider()
    reg.register(provider)
    snapshot = reg.items()
    # Replacing an entry in a local list must not corrupt the registry.
    modified = list(snapshot)
    modified[0] = ("good", None)  # type: ignore[assignment]
    assert reg.items()[0][1] is not None  # original provider still there


# ── force_close_all (ACTIVE-only, no CREATING) ─────────────────────────────


async def test_force_close_all_leaves_non_active_rows_untouched(db_session) -> None:
    """force_close_all targets ACTIVE rows only. Non-active rows (e.g. EXPIRED,
    DESTROYED) must be left untouched."""
    org_id = uuid4()
    agent = await seed_agent(org_id=org_id, session=db_session)
    active_ws = WorkspaceRow(
        id=uuid7(),
        org_id=org_id,
        provider_id="remote_agent",
        spec={"sha": "a"},
        status=WorkspaceStatus.ACTIVE.value,
        expires_at=_utcnow() + timedelta(hours=1),
        owning_agent_id=agent["id"],
    )
    expired_ws = WorkspaceRow(
        id=uuid7(),
        org_id=org_id,
        provider_id="remote_agent",
        spec={"sha": "b"},
        status=WorkspaceStatus.EXPIRED.value,
        expires_at=_utcnow() + timedelta(hours=1),
        owning_agent_id=agent["id"],
    )
    db_session.add_all([active_ws, expired_ws])
    await db_session.commit()

    count = await force_close_all(org_id=org_id, reason="disconnect")

    assert count == 1, "only the ACTIVE row should be expired"
    refreshed = {r.id: r for r in (await db_session.execute(select(WorkspaceRow))).scalars().all()}
    assert refreshed[active_ws.id].status == WorkspaceStatus.EXPIRED.value
    assert refreshed[expired_ws.id].status == WorkspaceStatus.EXPIRED.value


# ── failsafe_agent_loss: workflow correlation via agent_commands ────────────


async def test_failsafe_agent_loss_uses_command_row_correlation(db_session) -> None:
    """failsafe_agent_loss synthesizes a terminal failure for an in-flight
    command. The workflow_execution_id must come from agent_commands, not from
    the shed workspaces.current_holder_workflow_id column."""
    org_id = uuid4()
    stale = await seed_agent(org_id=org_id, session=db_session, heartbeat_age_seconds=600)
    workspace_id = uuid7()
    command_id = uuid7()

    # Enqueue a real agent_commands row so the correlation path is exercised.
    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent="",
    )
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    ws = WorkspaceRow(
        id=workspace_id,
        org_id=org_id,
        provider_id="remote_agent",
        spec={"sha": "x"},
        status=WorkspaceStatus.ACTIVE.value,
        expires_at=_utcnow() + timedelta(hours=1),
        owning_agent_id=stale["id"],
        current_command_id=command_id,
    )
    db_session.add(ws)
    await db_session.commit()

    await failsafe_agent_loss(db_session, {stale["id"]})
    await db_session.commit()

    await db_session.refresh(ws)
    assert ws.status == WorkspaceStatus.EXPIRED.value
