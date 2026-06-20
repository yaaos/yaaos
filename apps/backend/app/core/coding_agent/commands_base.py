"""Abstract base class for CodingAgent AgentDispatchCommands.

`CodingAgentCommand` sits between `AgentDispatchCommand` and concrete workflow
commands that invoke a coding-agent plugin (e.g. `domain/reviewer`'s CodeReview).
Its `@final dispatch`:
  1. Resolves the plugin by `self.plugin_id`.
  2. Calls `self.build_invocation(inputs, ctx, session=session)` (abstract) to
     produce the high-level `Invocation` intent.
  3. Delegates to `core/coding_agent.dispatch_invocation` (Layer 3 → Layer 2 → Layer 1).

Subclasses must NOT override `dispatch` — an `__init_subclass__` guard raises
`TypeError` at class-body execution if they try.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar, final
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ValidationError

from app.core.workflow import AgentDispatchCommand, Outcome

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.coding_agent.types import Invocation
    from app.core.workflow import CommandContext


class CodingAgentCommand(AgentDispatchCommand):
    """Abstract base for AgentDispatchCommands that invoke a coding-agent plugin.

    Each concrete subclass declares:
      - `kind` — unique workflow-command kind string.
      - `plugin_id` — identifies the registered `CodingAgentPlugin`.
      - `Inputs` / `Outputs` — Pydantic models; `Inputs` must carry `workspace_id`;
        `Outputs` must declare a `response: ExpectedResponse` field.
      - `ExpectedResponse` — the Pydantic model the agent's JSON output must match.
        Auto-injected as `Invocation.context["output_schema"]` by `@final dispatch`.
        Validated by the default `handle_response` on a `completed_success` event.
      - `build_invocation` — pure async method that builds an `Invocation` object.

    `needs_claim` is always True for this branch: the workspace must be atomically
    claimed before the coding-agent subprocess is launched.
    `recovers_failure_label` can be set on subclasses to register a recovery policy.
    """

    plugin_id: ClassVar[str]
    ExpectedResponse: ClassVar[type[BaseModel]]
    needs_claim: ClassVar[bool] = True
    recovers_failure_label: ClassVar[str | None] = None
    restart_safe: ClassVar[bool] = True

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "dispatch" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} cannot override @final dispatch "
                "on CodingAgentCommand. Implement build_invocation instead."
            )

    @abstractmethod
    async def build_invocation(
        self,
        inputs: object,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> Invocation:
        """Build the high-level Invocation intent for this coding-agent call.

        Pure async — may query DB via `session` but must not commit.
        The Invocation is passed to the registered plugin's `compile_invocation`
        to produce the concrete exec block.
        """
        ...

    async def handle_response(
        self,
        output: str,
        ctx: CommandContext,
    ) -> Outcome:
        """Parse the agent's JSON output string and return a typed Outcome.

        Called by the engine (`_handle_agent_event_impl`) on `completed_success`
        events. `output` is `outputs["output"]` from the enriched agent event —
        the parsed skill stdout after run-sink extraction.

        Default: validates `output` against `ExpectedResponse.model_validate_json`.
        On success returns `Outcome.success(outputs=self.Outputs(response=parsed))`.
        On `ValidationError` returns `Outcome.failure(reason=..., retryable=False)`
        — schema violations are not transient; retrying would produce the same bad
        output. Subclasses may override to extend or replace this behaviour.

        The `output: str` signature (not `RunResult`) avoids a circular import:
        `core.coding_agent` → `core.workflow`, so the reverse import is forbidden;
        `handle_response` only needs the output string, not the full `RunResult`.
        """
        cls = type(self)
        try:
            parsed = cls.ExpectedResponse.model_validate_json(output)
            return Outcome.success(outputs=cls.Outputs(response=parsed))  # type: ignore[call-arg]
        except ValidationError as exc:
            return Outcome.failure(
                reason=f"{cls.kind} response schema violation: {exc}",
                retryable=False,
            )

    @final
    async def dispatch(
        self,
        inputs: object,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> UUID:
        """Resolve the plugin, build the invocation, and delegate to
        `core/coding_agent.dispatch_invocation` (Layer 3). Returns the minted
        `command_id`; durable iff the caller's transaction commits.

        Auto-injects `ExpectedResponse.model_json_schema()` into the invocation
        context under the `output_schema` key so the skill prompt carries the
        validated response contract without each subclass having to set it.
        """
        from app.core.coding_agent.service import (  # noqa: PLC0415
            dispatch_invocation,
            get_plugin,
        )

        plugin = get_plugin(self.plugin_id)
        try:
            invocation = await self.build_invocation(inputs, ctx, session=session)
            # Inject output schema so the skill prompt carries the exact contract.
            cls = type(self)
            invocation = invocation.model_copy(
                update={
                    "context": {
                        **invocation.context,
                        "output_schema": cls.ExpectedResponse.model_json_schema(),
                    }
                }
            )
            workspace_id = inputs.workspace_id  # Inputs Protocol requirement
            return await dispatch_invocation(
                workspace_id=workspace_id,
                invocation=invocation,
                plugin=plugin,
                ctx=ctx,
                session=session,
            )
        except Exception as exc:
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            raise
