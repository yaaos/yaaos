"""IoC seam between `core/agent_gateway` and `core/workspace`.

`WorkspaceAgentReportSink` is the Protocol that workspace implements and
registers here at import time. agent_gateway calls the registered sink for
all workspace-state work; it never imports `core/workspace` itself.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel


class WorkspaceEventReport(BaseModel):
    """Wire payload agent_gateway passes into the sink for a WorkspaceEvent."""

    workspace_id: UUID
    command_id: UUID | None
    kind: str


class WorkspaceEventOutcome(BaseModel):
    """Result the sink returns after applying a workspace event.

    Handlers consume this and map to the wire response. The sink never raises
    a workspace-internal exception across the boundary — errors surface as an
    accepted=False outcome.
    """

    resolved_status: str | None
    accepted: bool


class WorkspaceAgentReportSink(Protocol):
    """Contract that `core/workspace` implements and registers.

    Three operations cover all workspace-state access agent_gateway needs:
    - `reconcile_heartbeat` — pure read; returns ids the agent should forget.
    - `apply_workspace_event` — applies kind→status map; returns outcome VO.
    - `resolve_claim` — pure read; returns the workflow holding a command.
    """

    async def reconcile_heartbeat(
        self,
        reported_ids: set[UUID],
        session: object,
    ) -> set[UUID]:
        """Return workspace ids the agent reports that are unknown or destroyed.

        Reads workspace statuses; no writes. `session` is an `AsyncSession`.
        """
        ...

    async def apply_workspace_event(
        self,
        report: WorkspaceEventReport,
        session: object,
    ) -> WorkspaceEventOutcome:
        """Apply agent-reported kind → control-plane status mapping.

        Writes workspace status when the kind maps to one. Returns an outcome
        VO; never raises across the boundary.
        """
        ...

    async def resolve_claim(
        self,
        command_id: UUID,
        session: object,
    ) -> UUID | None:
        """Return the `current_holder_workflow_id` for the workspace holding
        `command_id`, or None if no workspace is claimed by that command.
        """
        ...


# ── Single-slot registry ───────────────────────────────────────────────

_SINK: WorkspaceAgentReportSink | None = None


def register_report_sink(sink: WorkspaceAgentReportSink) -> None:
    """Register the module-global sink. Called by `core/workspace.__init__`
    at import time.

    Idempotent for the same instance; raises `RuntimeError` on a conflicting
    re-registration so a double-wiring bug surfaces at boot instead of
    silently swapping the singleton. Tests swap stubs via `clear_report_sink`
    first.
    """
    global _SINK
    if _SINK is not None and _SINK is not sink:
        raise RuntimeError("WorkspaceAgentReportSink already registered — clear it before re-registering")
    _SINK = sink


def get_report_sink() -> WorkspaceAgentReportSink:
    """Return the registered sink.

    Raises `RuntimeError` when no sink has been registered — this indicates
    a startup wiring problem (workspace module not imported before agent_gateway
    service functions are called).
    """
    if _SINK is None:
        raise RuntimeError(
            "WorkspaceAgentReportSink not registered — "
            "import app.core.workspace before calling agent_gateway service functions"
        )
    return _SINK


def clear_report_sink() -> None:
    """Reset the registry slot. Used in tests to swap stub sinks."""
    global _SINK
    _SINK = None
