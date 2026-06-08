"""Concrete implementation of `AgentRunSink`.

Registered by `domain/coding_agent.__init__` at import time.
Fires only on `InvokeClaudeCode` terminal events — all other command kinds
are silently no-ops so the sink can be registered without per-kind checks
in `agent_gateway`.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.coding_agent.run_service import finalize_run, get_run_id_for_command

log = structlog.get_logger("domain.coding_agent.run_sink")

# Only this command kind produces run rows.
_INVOKE_CLAUDE_CODE_KIND = "InvokeClaudeCode"


class CodingAgentRunSinkImpl:
    """Finalizes `coding_agent_runs` rows on `InvokeClaudeCode` terminal events."""

    async def handle_terminal_event(
        self,
        command_id: UUID,
        command_kind: str,
        event_kind: str,
        outputs: dict,  # type: ignore[type-arg]
        session: AsyncSession,
    ) -> None:
        """Finalize the run row for this `InvokeClaudeCode` terminal event.

        No-ops silently for all other command kinds — provision/cleanup/
        writefiles/refreshauth do not have run rows.

        `outputs` carries `exit_code` (int | None) and `stdout` (str | None)
        from `AgentEvent.outputs` (set by `apps/agent/internal/command/results.go`).

        Token usage and activity are NULL.
        """
        if command_kind != _INVOKE_CLAUDE_CODE_KIND:
            return

        run_id = await get_run_id_for_command(command_id, session=session)
        if run_id is None:
            # No run row exists for this command — log and skip.
            log.warning(
                "coding_agent.run_sink.no_run_row",
                command_id=str(command_id),
                command_kind=command_kind,
            )
            return

        status = "success" if event_kind == "completed_success" else "failure"
        exit_code_raw = outputs.get("exit_code")
        exit_code: int | None = int(exit_code_raw) if exit_code_raw is not None else None

        await finalize_run(
            run_id,
            usage=None,
            activity=None,
            exit_code=exit_code,
            status=status,
            session=session,
        )
        log.info(
            "coding_agent.run.finalized_via_sink",
            run_id=str(run_id),
            command_id=str(command_id),
            status=status,
            exit_code=exit_code,
        )
