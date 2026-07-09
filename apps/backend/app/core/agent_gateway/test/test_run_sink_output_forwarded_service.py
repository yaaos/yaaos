"""Service test: the real coding-agent run sink forwards `output` and strips `stdout`.

Drives `record_agent_event` for an `InvokeClaudeCode` `completed_success` event
with a real `coding_agent_runs` row in place. Verifies:

(a) The sink's `output` key (parsed skill stdout from `plugin.parse_result`) is
    forwarded into the `HANDLE_AGENT_EVENT` task args as `outputs["output"]`.
(b) Raw `stdout` is stripped from the forwarded outputs so downstream run
    steps cannot accidentally read the stale key.

The boot-time assert in web.py / worker.py guarantees the sink is always
registered in production. This test exercises the now-unconditional call path
in `record_agent_event`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest

from app.core.agent_gateway import (
    AgentEvent,
    AgentEventKind,
    Artifact,
    InvokeClaudeCodeCommand,
    InvokeClaudeCodeLimits,
    RepoRef,
    enqueue_command,
    record_agent_event,
    register_report_sink,
)
from app.core.agent_gateway.report_sink import clear_report_sink
from app.core.coding_agent import create_run
from app.core.tasks import get_pending_outbox_payloads

# ── Stub report sink ───────────────────────────────────────────────────


class _NoopReportSink:
    """Minimal WorkspaceAgentReportSink stub — all methods are no-ops."""

    async def reconcile_heartbeat(self, reported_ids: set[UUID], session: object) -> set[UUID]:
        return set()

    async def apply_workspace_event(self, report: object, session: object) -> object:
        from app.core.agent_gateway.report_sink import WorkspaceEventOutcome  # noqa: PLC0415

        return WorkspaceEventOutcome(resolved_status=None, accepted=True)

    async def materialise_provision_success(
        self,
        *,
        command_id: UUID,
        agent_id: UUID,
        session: object,
    ) -> None:
        return None

    async def resolve_claim(self, command_id: UUID, session: object) -> UUID | None:
        return None

    async def owning_agent_for_workspace(self, workspace_id: UUID, session: object) -> UUID | None:
        return None

    async def owning_agent_for_command(self, command_id: UUID, session: object) -> UUID | None:
        return None

    async def handle_agent_loss(self, agent_ids: set[UUID], session: object) -> None:
        return None

    async def release_command_claim(self, command_id: UUID, session: object) -> None:
        return None


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_report_sink():
    """Install the no-op report sink; restore the prior one on teardown."""
    try:
        from app.core.agent_gateway import get_report_sink as _get  # noqa: PLC0415

        prior = _get()
    except RuntimeError:
        prior = None

    clear_report_sink()
    register_report_sink(_NoopReportSink())
    yield
    clear_report_sink()
    if prior is not None:
        register_report_sink(prior)


# ── Helpers ────────────────────────────────────────────────────────────


async def _seed_invoke_command(
    org_id: UUID,
    *,
    run_id: UUID,
    session: object,
) -> UUID:
    """Enqueue an InvokeClaudeCodeCommand and a linked coding_agent_runs row.

    Returns the command_id. The run row is seeded so the real
    CodingAgentRunSinkImpl can resolve it and call plugin.parse_result.
    """
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: PLC0415

    assert isinstance(session, AsyncSession)

    cmd_id = uuid7()
    workspace_id = uuid4()
    command = InvokeClaudeCodeCommand(
        command_id=cmd_id,
        workspace_id=workspace_id,
        traceparent="",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/example/repo.git",
            head_sha="deadbeef",
        ),
        invocation={"prompt": "review the code"},
        limits=InvokeClaudeCodeLimits(wallclock_seconds=300),
        skill_path=".claude/skills/pr_review/SKILL.md",
    )
    await enqueue_command(
        org_id=org_id,
        command=command,
        session=session,
        run_id=run_id,
    )

    # Seed the coding_agent_runs row — the real sink resolves it to determine
    # the plugin and call parse_result. plugin_id "claude_code" matches the
    # stub-wrapped plugin registered by _canonical_registries.
    await create_run(
        org_id=org_id,
        run_id=run_id,
        stage_execution_id=uuid4(),
        agent_command_id=cmd_id,
        command_kind="InvokeClaudeCode",
        plugin_id="claude_code",
        session=session,
    )

    return cmd_id


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_real_sink_forwards_output_and_strips_stdout(db_session) -> None:
    """The real CodingAgentRunSinkImpl:
    (a) produces an `output` key in the HANDLE_AGENT_EVENT task args (populated
        by plugin.parse_result from the raw `stdout`).
    (b) strips `stdout` from the forwarded outputs so downstream steps read
        the canonical `output` key only.
    """
    org_id = uuid4()
    run_id = uuid4()

    cmd_id = await _seed_invoke_command(org_id, run_id=run_id, session=db_session)
    await db_session.commit()

    # The stub plugin's parse_result sets output=stdout, error_message=None.
    raw_stdout = "## Review findings\nNo issues found."

    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outputs={"exit_code": 0, "stdout": raw_stdout},
        reported_at=datetime.now(UTC),
        traceparent="",
    )

    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, session=db_session)

    payloads = await get_pending_outbox_payloads(db_session)
    # `record_agent_event` also enqueues the seeding outbox row; filter to
    # the run engine's own consumer task name.
    handle_payloads = [p for p in payloads if p.get("task_name") == "pipelines.handle_agent_event"]
    assert len(handle_payloads) == 1, (
        f"expected exactly one pipelines.handle_agent_event; got {handle_payloads}"
    )

    task_outputs = handle_payloads[0]["args"]["outputs"]

    # (a) sink-derived `output` key is present and carries the parsed stdout.
    assert task_outputs.get("output") == raw_stdout, (
        f"expected output={raw_stdout!r}, got {task_outputs.get('output')!r}"
    )

    # (b) raw `stdout` is stripped — downstream steps must not read the stale key.
    assert "stdout" not in task_outputs, f"stdout should be stripped; got keys: {list(task_outputs)}"


@pytest.mark.asyncio
@pytest.mark.service
async def test_agent_event_artifact_fields_forwarded_to_outbox_payload(db_session) -> None:
    """`AgentEvent.artifact` / `artifact_error` ride the `HANDLE_AGENT_EVENT`
    task args' `outputs` dict — the durable outbox row is where the artifact
    lives until an engine reads it."""
    org_id = uuid4()
    run_id = uuid4()

    cmd_id = await _seed_invoke_command(org_id, run_id=run_id, session=db_session)
    await db_session.commit()

    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outputs={"exit_code": 0, "stdout": "## Requirements\nDone."},
        reported_at=datetime.now(UTC),
        traceparent="",
        artifact=Artifact(body="## Requirements\nDone."),
    )

    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, session=db_session)

    payloads = await get_pending_outbox_payloads(db_session)
    # `record_agent_event` also enqueues the seeding outbox row; filter to
    # the run engine's own consumer task name.
    handle_payloads = [p for p in payloads if p.get("task_name") == "pipelines.handle_agent_event"]
    assert len(handle_payloads) == 1, (
        f"expected exactly one pipelines.handle_agent_event; got {handle_payloads}"
    )

    task_outputs = handle_payloads[0]["args"]["outputs"]
    assert task_outputs.get("artifact") == {"body": "## Requirements\nDone."}
    assert "artifact_error" not in task_outputs


@pytest.mark.asyncio
@pytest.mark.service
async def test_agent_event_artifact_error_forwarded_to_outbox_payload(db_session) -> None:
    """`artifact_error` rides the forwarded outputs when the artifact was
    over-cap (or otherwise unreadable) — distinct from a null artifact."""
    org_id = uuid4()
    run_id = uuid4()

    cmd_id = await _seed_invoke_command(org_id, run_id=run_id, session=db_session)
    await db_session.commit()

    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outputs={"exit_code": 0, "stdout": ""},
        reported_at=datetime.now(UTC),
        traceparent="",
        artifact_error="artifact exceeds 2097152 bytes (was 3000000)",
    )

    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, session=db_session)

    payloads = await get_pending_outbox_payloads(db_session)
    # `record_agent_event` also enqueues the seeding outbox row; filter to
    # the run engine's own consumer task name.
    handle_payloads = [p for p in payloads if p.get("task_name") == "pipelines.handle_agent_event"]
    assert len(handle_payloads) == 1, (
        f"expected exactly one pipelines.handle_agent_event; got {handle_payloads}"
    )

    task_outputs = handle_payloads[0]["args"]["outputs"]
    assert task_outputs.get("artifact_error") == "artifact exceeds 2097152 bytes (was 3000000)"
    assert "artifact" not in task_outputs
