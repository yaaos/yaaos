"""Service tests — claim-time credential hydration in claim_next.

Covers:
- ConfigUpdate hydration: key set → persisted payload credential-free,
  claimed DTO hydrated (both halves asserted together).
- Key cleared mid-queue: claim delivers empty api_keys.
- Hydrator absent for a kind: payload passes verbatim.
- InvokeCodex hydration failure: retires row to done, synthesizes a
  completed_failure AgentEvent; queue is empty so returns None.
- Retire-loop cap (_HYDRATION_RETIRE_CAP): after N retirements per call
  the loop returns None without touching remaining rows (starvation guard).
- ConfigUpdate hydration failure: leaves the row pending, returns None.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4, uuid7

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway.api_key_provider import get_api_key_secrets_provider
from app.core.agent_gateway.hydrators import (
    _HYDRATORS,
    CredentialHydrationError,
    HydrationContext,
    register_command_hydrator,
)
from app.core.agent_gateway.models import AgentCommandRow
from app.core.agent_gateway.service import (
    _HYDRATION_RETIRE_CAP,
    claim_next,
    enqueue_config_update_for_agent,
)
from app.core.agent_gateway.types import AgentCommandKind, ConfigUpdateCommand
from app.core.audit_log import Actor
from app.testing.e2e_setup import seed_agent, seed_org

# ── Inline ConfigUpdate hydrator ─────────────────────────────────────────────


async def _hydrate_config_update_for_test(
    payload: dict, ctx: HydrationContext, session: AsyncSession
) -> dict:
    """Replicate core/coding_agent's ConfigUpdate hydrator for tests.

    Uses the registered ApiKeySecretsProvider (same IoC seam as production)
    without a cross-module submodule import.
    """

    org_id: UUID = ctx.org_id
    out = dict(payload)
    provider = get_api_key_secrets_provider()
    if provider is None:
        return out
    api_keys = await provider(org_id, session=session)
    config = dict(out.get("config") or {})
    config["api_keys"] = api_keys
    out["config"] = config
    return out


# ── Test isolation fixture ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_hydrators():
    """Save, clear, then restore the hydrator registry around each test.

    Production hydrators register once, at import time, as a side effect — so a
    fixture that merely cleared the registry would unregister them for the rest of
    the pytest process. Saving and restoring keeps them intact regardless of test
    execution order.
    """
    _prior = dict(_HYDRATORS)
    _HYDRATORS.clear()
    yield
    _HYDRATORS.clear()
    _HYDRATORS.update(_prior)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _minimal_invoke_codex_payload(
    *,
    command_id: UUID,
    workspace_id: UUID,
    run_id: UUID,
) -> dict:
    """Return a minimal payload dict for an InvokeCodex AgentCommandRow.

    `invocation` nests the exec block (argv, env, stdin) as required by
    `InvokeCodexFields`/`InvokeCodexCommand`.  Putting those keys at the
    top level is a schema violation that causes Pydantic validation to fail
    at claim time.
    """
    return {
        "kind": "InvokeCodex",
        "command_id": str(command_id),
        "workspace_id": str(workspace_id),
        "traceparent": "",
        "completion_token": None,
        "run_id": str(run_id),
        "skill_path": ".codex/skills/test/SKILL.md",
        "limits": {"wallclock_seconds": 900},
        "invocation": {
            "argv": ["codex", "exec", "--model", "codex-mini-latest", "--quiet"],
            "env": {},
            "stdin": "test prompt",
        },
        "output_schema_json": None,
    }


async def _insert_invoke_codex_row(
    db_session: AsyncSession,
    *,
    org_id: UUID,
    agent_id: UUID,
    workspace_id: UUID,
    run_id: UUID,
) -> AgentCommandRow:
    """Insert a pending InvokeCodex AgentCommandRow directly for testing."""
    command_id = uuid7()
    payload = _minimal_invoke_codex_payload(
        command_id=command_id,
        workspace_id=workspace_id,
        run_id=run_id,
    )
    row = AgentCommandRow(
        id=command_id,
        org_id=org_id,
        workspace_id=workspace_id,
        run_id=run_id,
        command_kind=AgentCommandKind.INVOKE_CODEX,
        status="pending",
        agent_id=agent_id,
        payload=payload,
    )
    db_session.add(row)
    await db_session.flush()
    return row


# ── ConfigUpdate hydration ───────────────────────────────────────────────────


@pytest.mark.service
async def test_config_update_hydration_injects_api_keys(db_session) -> None:
    """ConfigUpdate hydration: persisted row is credential-free; claimed DTO
    carries real api_keys.

    Both halves are verified in the same test:
    - After enqueue: row.payload.config.api_keys == {}.
    - After claim_next: returned DTO.config.api_keys == {"anthropic": "sk-test"}.
    """
    import app.core.api_keys as api_keys  # noqa: PLC0415

    # Register the test ConfigUpdate hydrator (the autouse fixture cleared all
    # hydrators above); uses the same IoC seam as the production hydrator.
    register_command_hydrator("ConfigUpdate", _hydrate_config_update_for_test)

    org_id = await seed_org()
    agent_row = await seed_agent(org_id=org_id)
    agent_id = agent_row["id"]

    # Set an API key; after the rework the enqueued row must NOT embed it.
    await api_keys.set(org_id, "anthropic", "sk-test", actor=Actor.system(), session=db_session)
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    # Half 1: persisted payload has no credentials.
    row = (
        (
            await db_session.execute(
                select(AgentCommandRow)
                .where(
                    AgentCommandRow.org_id == org_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                    AgentCommandRow.status == "pending",
                )
                .order_by(AgentCommandRow.id)
                .limit(1)
            )
        )
        .scalars()
        .one_or_none()
    )
    assert row is not None, "Expected a pending ConfigUpdate row"
    persisted_keys = (row.payload or {}).get("config", {}).get("api_keys", {})
    assert persisted_keys == {}, f"Persisted payload must have api_keys={{}}; got {persisted_keys!r}"

    # Half 2: claimed DTO has credentials hydrated in.
    cmd = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert cmd is not None, "Expected a claimed ConfigUpdate command"
    assert isinstance(cmd, ConfigUpdateCommand)
    wire = cmd.config.model_dump(mode="json")
    assert wire["api_keys"].get("anthropic") == "sk-test", (
        f"Claimed DTO must carry hydrated api_keys; got {wire['api_keys']}"
    )


@pytest.mark.service
async def test_config_update_hydration_empty_when_no_key(db_session) -> None:
    """When no API key is stored, the claimed DTO carries `api_keys == {}`."""
    register_command_hydrator("ConfigUpdate", _hydrate_config_update_for_test)

    org_id = await seed_org()
    agent_row = await seed_agent(org_id=org_id)
    agent_id = agent_row["id"]

    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    cmd = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert cmd is not None
    assert isinstance(cmd, ConfigUpdateCommand)
    wire = cmd.config.model_dump(mode="json")
    assert wire["api_keys"] == {}, (
        f"No key stored — claimed DTO must have api_keys={{}}; got {wire['api_keys']}"
    )


# ── No hydrator for kind ─────────────────────────────────────────────────────


@pytest.mark.service
async def test_no_hydrator_passes_payload_verbatim(db_session) -> None:
    """When no hydrator is registered for a kind, claim_next returns the DTO
    deserialised from the persisted payload without modification."""
    org_id = await seed_org()
    agent_row = await seed_agent(org_id=org_id)
    agent_id = agent_row["id"]

    # No ConfigUpdate hydrator registered (autouse fixture cleared them all).
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    cmd = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert cmd is not None, "Expected a claimed command even without a hydrator"
    assert isinstance(cmd, ConfigUpdateCommand)
    # No hydrator means api_keys is whatever was stored (empty by enqueue logic).
    wire = cmd.config.model_dump(mode="json")
    assert wire["api_keys"] == {}, "Verbatim path must return what was stored"


# ── InvokeCodex api-key mode success path (shape lock) ──────────────────────


@pytest.mark.service
async def test_invoke_codex_api_key_mode_shape_validates(db_session) -> None:
    """InvokeCodex api-key mode: correct `invocation` dict shape → Pydantic
    validates and claim_next returns InvokeCodexCommand.

    This test locks the wire shape: `argv`/`env`/`stdin` must be nested under
    `invocation`, not at the payload top level.
    """
    from app.core.agent_gateway.types import InvokeCodexCommand  # noqa: PLC0415

    org_id = await seed_org()
    agent_row = await seed_agent(org_id=org_id)
    agent_id = agent_row["id"]
    workspace_id = uuid4()
    run_id = uuid4()

    await _insert_invoke_codex_row(
        db_session,
        org_id=org_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        run_id=run_id,
    )
    await db_session.flush()

    # No InvokeCodex hydrator registered (autouse fixture cleared them all).
    # The no-hydrator path passes the payload verbatim; Pydantic validates the shape.
    cmd = await claim_next(
        agent_id,
        lifecycle="active",
        new_workspaces=0,
        workspace_ids=[workspace_id],
        wait_seconds=0,
        session=db_session,
    )

    assert cmd is not None, "Expected a claimed InvokeCodex command"
    assert isinstance(cmd, InvokeCodexCommand), f"Expected InvokeCodexCommand, got {type(cmd).__name__}"
    assert cmd.invocation == {
        "argv": ["codex", "exec", "--model", "codex-mini-latest", "--quiet"],
        "env": {},
        "stdin": "test prompt",
    }, f"Unexpected invocation dict: {cmd.invocation!r}"
    assert cmd.skill_path == ".codex/skills/test/SKILL.md"


# ── Run-bearing kind hydration failure ──────────────────────────────────────


@pytest.mark.service
async def test_invoke_codex_hydration_failure_retires_row(
    db_session,
) -> None:
    """A CredentialHydrationError on InvokeCodex retires that row to done
    and returns None when no other eligible command exists.

    Note: a ConfigUpdate in the same queue would be claimed FIRST (bucket-1
    priority over bucket-3 workspace commands), so this test isolates just the
    InvokeCodex retirement path — no ConfigUpdate in the queue.
    """

    # InvokeCodex: always fail hydration.
    async def failing_hydrator(payload, ctx, session):
        raise CredentialHydrationError("test: per-user auth not supported")

    register_command_hydrator("InvokeCodex", failing_hydrator)

    org_id = await seed_org()
    agent_row = await seed_agent(org_id=org_id)
    agent_id = agent_row["id"]
    workspace_id = uuid4()  # no FK constraint on workspace_id in agent_commands
    run_id = uuid4()

    invoke_row = await _insert_invoke_codex_row(
        db_session,
        org_id=org_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        run_id=run_id,
    )
    await db_session.flush()

    # Claim with the InvokeCodex workspace in workspace_ids.
    cmd = await claim_next(
        agent_id,
        lifecycle="active",
        new_workspaces=0,
        workspace_ids=[workspace_id],
        wait_seconds=0,
        session=db_session,
    )

    # The InvokeCodex row must be retired; queue is empty so cmd is None.
    await db_session.refresh(invoke_row)
    assert invoke_row.status == "done", (
        f"InvokeCodex row must be retired to done after hydration failure; got {invoke_row.status!r}"
    )
    assert cmd is None, f"Expected None after retirement with empty queue; got {type(cmd).__name__}"


@pytest.mark.service
async def test_invoke_codex_hydration_failure_with_run_id_synthesizes_failure_event(
    db_session,
) -> None:
    """A hydration failure on a row with run_id enqueues a completed_failure
    AgentEvent outbox entry for the run engine."""
    from sqlalchemy import text  # noqa: PLC0415

    async def failing_hydrator(payload, ctx, session):
        raise CredentialHydrationError("no api key")

    register_command_hydrator("InvokeCodex", failing_hydrator)

    org_id = await seed_org()
    agent_row = await seed_agent(org_id=org_id)
    agent_id = agent_row["id"]
    workspace_id = uuid4()
    run_id = uuid4()

    invoke_row = await _insert_invoke_codex_row(
        db_session,
        org_id=org_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        run_id=run_id,
    )
    await db_session.flush()

    await claim_next(
        agent_id,
        lifecycle="active",
        new_workspaces=0,
        workspace_ids=[workspace_id],
        wait_seconds=0,
        session=db_session,
    )

    # The invoke row should be retired.
    await db_session.refresh(invoke_row)
    assert invoke_row.status == "done", "InvokeCodex row must be retired"

    # An outbox entry with the failure payload should have been enqueued.
    # We check the outbox_entries table directly.
    outbox_rows = (
        await db_session.execute(text("SELECT payload FROM outbox_entries ORDER BY id DESC LIMIT 10"))
    ).fetchall()
    # At least one outbox row should mention the run_id.
    run_id_str = str(run_id)
    found = any(
        run_id_str in (json.dumps(r[0]) if isinstance(r[0], dict) else str(r[0])) for r in outbox_rows
    )
    assert found, (
        f"Expected an outbox entry with run_id={run_id_str!r}; outbox rows: {[r[0] for r in outbox_rows]}"
    )


# ── Retire-loop cap (starvation guard) ───────────────────────────────────────


@pytest.mark.service
async def test_hydration_retire_cap_returns_none_after_n_retirements(
    db_session,
) -> None:
    """After _HYDRATION_RETIRE_CAP row retirements in a single claim_next call,
    the function returns None even when more eligible rows exist.

    This prevents a misconfigured org from starving the claim loop indefinitely.
    """

    async def always_fail(payload, ctx, session):
        raise CredentialHydrationError("forced failure for cap test")

    register_command_hydrator("InvokeCodex", always_fail)

    org_id = await seed_org()
    agent_row = await seed_agent(org_id=org_id)
    agent_id = agent_row["id"]
    workspace_id = uuid4()
    inserted_rows: list[AgentCommandRow] = []

    # Insert _HYDRATION_RETIRE_CAP + 1 rows so there are always more to try.
    for _ in range(_HYDRATION_RETIRE_CAP + 1):
        row = await _insert_invoke_codex_row(
            db_session,
            org_id=org_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            run_id=uuid4(),
        )
        inserted_rows.append(row)
    await db_session.flush()

    result = await claim_next(
        agent_id,
        lifecycle="active",
        new_workspaces=0,
        workspace_ids=[workspace_id],
        wait_seconds=0,
        session=db_session,
    )

    assert result is None, f"claim_next must return None after reaching the retire cap; got {result!r}"

    # Exactly _HYDRATION_RETIRE_CAP rows should have been retired.
    retired_count = 0
    for row in inserted_rows:
        await db_session.refresh(row)
        if row.status == "done":
            retired_count += 1
    assert retired_count == _HYDRATION_RETIRE_CAP, (
        f"Expected exactly {_HYDRATION_RETIRE_CAP} rows retired; got {retired_count}"
    )

    # The remaining row (_HYDRATION_RETIRE_CAP + 1 th) must still be pending.
    still_pending = sum(1 for r in inserted_rows if r.status == "pending")
    assert still_pending >= 1, "At least one row must be left untouched by the cap"


# ── ConfigUpdate hydration failure ───────────────────────────────────────────


@pytest.mark.service
async def test_config_update_hydration_failure_leaves_row_pending_returns_none(
    db_session,
) -> None:
    """A CredentialHydrationError from a ConfigUpdate hydrator reverts the row
    to pending status and returns None — the agent retries on the next cycle."""

    async def always_fail(payload, ctx, session):
        raise CredentialHydrationError("config update hydration forced failure")

    register_command_hydrator("ConfigUpdate", always_fail)

    org_id = await seed_org()
    agent_row = await seed_agent(org_id=org_id)
    agent_id = agent_row["id"]

    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    # Capture the row id before claiming.
    rows = (
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.org_id == org_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                    AgentCommandRow.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    target_row = rows[0]

    result = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )

    assert result is None, "ConfigUpdate hydration failure must return None (not a DTO)"

    # Row must be reverted to pending, not retired to done.
    await db_session.refresh(target_row)
    assert target_row.status == "pending", (
        f"ConfigUpdate row must stay pending after hydration failure; got status={target_row.status!r}"
    )
