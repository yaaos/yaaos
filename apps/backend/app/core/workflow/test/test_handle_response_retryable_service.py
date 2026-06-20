"""Service tests verifying that `retryable=False` from `handle_response`
skips the Tier-2 retry budget and transitions the workflow to FAILED.

Uses a plain `AgentDispatchCommand` subclass with `handle_response` added
directly (the engine uses duck-typing), mimicking how `CodingAgentCommand`
works without requiring a real coding-agent plugin registration.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4, uuid7

import pytest
from pydantic import BaseModel, ConfigDict

from app.core.agent_gateway import CleanupWorkspaceCommand, enqueue_command
from app.core.workflow import (
    AgentDispatchCommand,
    CommandContext,
    Empty,
    Outcome,
    RetryPolicy,
    TerminalAction,
    Workflow,
    WorkflowState,
    get_execution_summary,
    step,
)
from app.testing.workflow_harness import scoped_engine, scoped_workflow

pytestmark = pytest.mark.service

_ORG_ID = uuid4()


class _EchoResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    msg: str


class _EchoOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)
    response: _EchoResponse = _EchoResponse(msg="")


class _SchemaCmd(AgentDispatchCommand):
    """AgentDispatchCommand that adds `handle_response` via duck-type.

    Mimics CodingAgentCommand behavior without needing a real plugin.
    `dispatch` enqueues a real agent_commands row; `handle_response`
    validates a JSON string against `_EchoResponse`.
    """

    kind = "_test_schema_cmd"
    Inputs = Empty
    Outputs = _EchoOutputs

    async def execute(self, inputs: Empty, ctx: CommandContext) -> Outcome:  # type: ignore[override]
        return Outcome.failure(reason="not the execute path")

    async def dispatch(self, inputs: Empty, ctx: CommandContext, *, session) -> UUID:  # type: ignore[override]
        command_id = uuid7()
        cmd = CleanupWorkspaceCommand(
            command_id=command_id,
            workspace_id=uuid4(),
            traceparent=ctx.traceparent or "",
        )
        await enqueue_command(
            org_id=_ORG_ID,
            command=cmd,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
            session=session,
        )
        return command_id

    async def handle_response(self, output: str, ctx: CommandContext) -> Outcome:
        """Schema-validate the output against `_EchoResponse`."""
        from pydantic import ValidationError  # noqa: PLC0415

        try:
            parsed = _EchoResponse.model_validate_json(output)
            return Outcome.success(outputs=_EchoOutputs(response=parsed))
        except ValidationError as exc:
            return Outcome.failure(
                reason=f"_test_schema_cmd response schema violation: {exc}",
                retryable=False,
            )


def _make_workflow() -> Workflow:
    cmd_step = step(_SchemaCmd, retry_policy=RetryPolicy(max_attempts=3))
    return Workflow(
        name="_test_schema_retry",
        version=1,
        steps=(cmd_step,),
        entry=cmd_step,
        transitions={
            cmd_step: {
                "failure": TerminalAction.FAIL_WORKFLOW,
            }
        },
    )


@pytest.mark.asyncio
async def test_schema_violation_skips_retry_and_fails(db_session) -> None:
    """When `handle_response` returns `retryable=False`, the engine must skip
    the Tier-2 retry budget and immediately transition to FAILED — even when
    the step's `RetryPolicy.max_attempts > 1`.
    """
    from app.core.tasks import drain_once, enqueue, get_pending_task_names  # noqa: PLC0415
    from app.core.workflow import HANDLE_AGENT_EVENT  # noqa: PLC0415

    wf = _make_workflow()

    with scoped_workflow(wf):
        with scoped_engine() as eng:
            eng.register_workflow(wf)

            ticket_id = str(uuid4())

            wfx_id = await eng.start(
                workflow_name="_test_schema_retry",
                ticket_id=ticket_id,
                workflow_input=None,
                session=db_session,
            )
            await db_session.commit()

            from app.core.tasks import get_broker  # noqa: PLC0415

            async def _dispatcher(kind: str, payload: dict) -> None:
                assert kind == "taskiq_enqueue"
                decorated = get_broker().find_task(payload["task_name"])
                assert decorated is not None
                await decorated.original_func(**payload["args"])

            async def _drain(max_iter: int = 20) -> None:
                for _ in range(max_iter):
                    pending = await get_pending_task_names(db_session)
                    if not pending:
                        break
                    delivered = await drain_once(db_session, dispatcher=_dispatcher)
                    await db_session.commit()
                    if delivered == 0:
                        break

            # Initial drain — _SchemaCmd dispatches (AgentDispatch branch).
            await _drain()

            wfx_snap = await get_execution_summary(UUID(wfx_id), session=db_session)
            assert wfx_snap is not None
            assert wfx_snap.state == WorkflowState.AWAITING_AGENT.value
            assert wfx_snap.pending_agent_command_id is not None

            # Simulate a terminal event with invalid JSON output — handle_response
            # will fail with retryable=False.
            bad_output = json.dumps({"wrong_key": "bad"})
            await enqueue(
                HANDLE_AGENT_EVENT,
                args={
                    "workflow_execution_id": wfx_id,
                    "agent_command_id": str(wfx_snap.pending_agent_command_id),
                    "outcome_label": "success",
                    "outputs": {"output": bad_output},
                    "traceparent": None,
                },
                session=db_session,
            )
            await db_session.commit()
            await _drain()

            # Workflow must be FAILED — not retried (attempt 1 of 3 available).
            final = await get_execution_summary(UUID(wfx_id), session=db_session)
            assert final is not None
            assert final.state == WorkflowState.FAILED.value, (
                f"expected FAILED, got {final.state!r} — schema violation should bypass retry"
            )
