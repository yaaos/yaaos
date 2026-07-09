"""Types + `Action` Protocol for `domain/actions`.

`ActionContext` is flattened — it imports `domain/findings` only — so
`pipelines → actions → findings` stays strictly one-way. No tables: an
action's result persists on `stage_executions.action_result`, owned by
`domain/pipelines`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

from app.domain.findings import Finding

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class StageVerdict(BaseModel):
    """Actions-owned mirror of the recorded verdict."""

    finding_id: UUID
    status: Literal["fixed", "still_present", "user_overrode"] | None
    reply: str | None


class ActionContext(BaseModel):
    """Flattened control-plane context handed to `Action.execute`."""

    org_id: UUID
    ticket_id: UUID
    run_id: UUID
    repo_external_id: str
    vcs_plugin_id: str
    pr_external_id: str | None
    branch_name: str
    intake_point_id: str
    kickoff_input: str | None
    preceding_residuals: tuple[Finding, ...]
    preceding_verdicts: tuple[StageVerdict, ...]
    preceding_artifact_id: UUID | None


class ActionInfo(BaseModel):
    """Registry-listing shape for the "Add an action" picker."""

    action_id: str
    plugin_id: str | None
    label: str


@runtime_checkable
class Action(Protocol):
    """Synchronous deterministic control-plane stage executor.

    `execute` runs inside a SAVEPOINT the engine wraps around the call —
    the old `LocalCommand` shape. No parking, no boundary control, no
    artifact, no confidence. Bodies must be externally idempotent (a
    mid-body crash re-runs them)."""

    action_id: str
    plugin_id: str | None
    label: str
    Result: ClassVar[type[BaseModel]]

    async def execute(self, ctx: ActionContext, *, session: AsyncSession) -> BaseModel: ...


class ActionError(Exception):
    """Raised by `Action.execute` on hard failure — the run fails."""


class ActionNotFoundError(LookupError):
    """`action_id` not registered."""
