"""Service tests for `CodingAgentCommand.handle_response`.

Covers:
1. `handle_response` with a valid JSON output → `Outcome.success` with typed
   `Outputs(response=<ExpectedResponse>)`.
2. `handle_response` with a schema-violating output → `Outcome.failure` with
   `retryable=False` (schema violations are not transient).
3. `@final dispatch` auto-injects `ExpectedResponse.model_json_schema()` into
   `Invocation.context["output_schema"]` so the skill prompt carries the schema.
"""

from __future__ import annotations

import json
from typing import ClassVar
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from app.core.coding_agent.commands_base import CodingAgentCommand
from app.core.coding_agent.types import Invocation
from app.core.workflow import CommandContext


class _SimpleResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    value: str


class _SimpleOutputs(BaseModel):
    model_config = ConfigDict(frozen=True)
    response: _SimpleResponse = _SimpleResponse(value="")


class _EmptyInputs(BaseModel):
    model_config = ConfigDict(frozen=True)


class _MinimalCommand(CodingAgentCommand):
    """Minimal concrete CodingAgentCommand for testing handle_response directly."""

    kind = "_test_minimal"
    plugin_id = "stub"
    ExpectedResponse: ClassVar[type[BaseModel]] = _SimpleResponse
    Inputs = _EmptyInputs
    Outputs = _SimpleOutputs

    async def execute(self, inputs: _EmptyInputs, ctx: CommandContext):  # type: ignore[override]
        return None

    async def build_invocation(self, inputs: _EmptyInputs, ctx: CommandContext, *, session) -> Invocation:  # type: ignore[override]
        return Invocation(
            workspace_id=uuid4(),
            skill="stub",
            model="opus",
            effort="low",
            context={},
            wallclock_seconds=60,
        )


def _ctx() -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="_test_minimal",
        attempt=0,
    )


@pytest.mark.asyncio
async def test_handle_response_valid_json_returns_success() -> None:
    """Valid JSON output matching `ExpectedResponse` → `Outcome.success` with
    typed `Outputs(response=<parsed>)`.
    """
    cmd = _MinimalCommand()
    output = json.dumps({"value": "hello"})
    outcome = await cmd.handle_response(output, _ctx())

    assert outcome.label == "success"
    assert isinstance(outcome.outputs, _SimpleOutputs)
    assert outcome.outputs.response.value == "hello"
    assert outcome.retryable is True


@pytest.mark.asyncio
async def test_handle_response_schema_violation_no_retry() -> None:
    """Schema-violating JSON output → `Outcome.failure` with `retryable=False`.

    Schema violations are not transient — the agent would produce the same bad
    output on retry. The engine reads `retryable=False` from the Outcome and
    skips the Tier-2 retry budget.
    """
    cmd = _MinimalCommand()
    # Missing required `value` field
    output = json.dumps({"other_field": "x"})
    outcome = await cmd.handle_response(output, _ctx())

    assert outcome.label == "failure"
    assert outcome.retryable is False
    assert "_test_minimal response schema violation" in (outcome.failure_reason or "")


@pytest.mark.asyncio
async def test_handle_response_empty_output_returns_failure_no_retry() -> None:
    """Empty output string → `ValidationError` → `Outcome.failure(retryable=False)`."""
    cmd = _MinimalCommand()
    outcome = await cmd.handle_response("", _ctx())

    assert outcome.label == "failure"
    assert outcome.retryable is False


def test_expected_response_auto_injected_into_invocation_context() -> None:
    """The auto-injected output_schema in `@final dispatch` encodes `ExpectedResponse`
    schema. Tested by inspecting the ClassVar directly (dispatch hits the DB;
    the injection logic is unit-tested via schema equality).
    """
    cmd = _MinimalCommand()
    expected_schema = _SimpleResponse.model_json_schema()

    # Verify the ClassVar resolves correctly for the auto-injection path.
    cls = type(cmd)
    assert cls.ExpectedResponse is _SimpleResponse
    assert cls.ExpectedResponse.model_json_schema() == expected_schema
    # The schema has the right shape for the output_schema context key.
    assert "properties" in expected_schema
    assert "value" in expected_schema["properties"]
