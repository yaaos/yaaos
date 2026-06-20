"""Abstract base class for workspace-lifecycle AgentDispatchCommands.

`WorkspaceOpCommand` is the intermediate ABC between `AgentDispatchCommand`
and the concrete workspace commands (`ProvisionWorkspace` excluded — it
inherits from `AgentDispatchCommand` directly because no workspace row exists
yet). It provides a `@final dispatch` that delegates to `dispatch_via_workspace`
and an `__init_subclass__` guard that prevents subclasses from overriding it.

Concrete subclasses implement `build_command` to produce the specific
`AgentCommand` wire payload; returning `None` signals "nothing to dispatch"
and the engine short-circuits to `Outcome.success()` via `_NullDispatch`.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar, final
from uuid import UUID

from app.core.workflow import AgentDispatchCommand, Outcome, _NullDispatch
from app.core.workspace.dispatch import dispatch_via_workspace

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.agent_gateway import AgentCommand
    from app.core.workflow import CommandContext


class WorkspaceOpCommand(AgentDispatchCommand):
    """Abstract base for AgentDispatchCommands that operate on an existing workspace.

    Dispatch walks Layer 2 (`dispatch_via_workspace`). Subclasses implement
    `build_command` to return the concrete `AgentCommand` wire payload.

    `needs_claim` controls whether `try_claim` is called atomically during
    dispatch — workspace provisioning uses the claim; cleanup does not.
    `recovers_failure_label` binds this command as the recovery step for a
    given failure label (registered via `register_recovery_policy`).
    """

    needs_claim: ClassVar[bool] = False
    recovers_failure_label: ClassVar[str | None] = None
    restart_safe: ClassVar[bool] = True

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "dispatch" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} cannot override @final dispatch "
                "on WorkspaceOpCommand. Implement build_command instead."
            )

    async def execute(
        self,
        inputs: object,
        ctx: CommandContext,
        *,
        session: object = None,
    ) -> Outcome:
        """Stub execute — `WorkspaceOpCommand` commands are dispatched via `dispatch`,
        not `execute`. Returns `Outcome.success()` unconditionally. Subclasses may
        override for test-provider compatibility (override is permitted — only `dispatch`
        is guarded by `__init_subclass__`)."""
        del inputs, ctx, session
        return Outcome.success()

    @abstractmethod
    async def build_command(
        self,
        inputs: object,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> AgentCommand | None:
        """Build the AgentCommand payload for this workspace operation.

        Return `None` when there is nothing to dispatch (e.g. `workspace_id`
        is None in `CleanupWorkspace`). The `@final dispatch` raises
        `_NullDispatch` in that case so the engine short-circuits to success.
        """
        ...

    @final
    async def dispatch(
        self,
        inputs: object,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> UUID:
        """Enqueue the command via dispatch_via_workspace (Layer 2) and return
        the command_id. Raises `_NullDispatch` when `build_command` returns None.
        """
        cmd = await self.build_command(inputs, ctx, session=session)
        if cmd is None:
            raise _NullDispatch()
        return await dispatch_via_workspace(
            command=cmd,
            workspace_id=cmd.workspace_id,  # type: ignore[attr-defined]
            ctx=ctx,
            session=session,
            claim_workspace=self.needs_claim,
        )
