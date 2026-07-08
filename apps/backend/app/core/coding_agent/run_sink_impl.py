"""Concrete implementation of `AgentRunSink`.

Registered by `core/coding_agent.__init__` at import time.
Fires only on `InvokeClaudeCode` terminal events — all other command kinds
are silently no-ops so the sink can be registered without per-kind checks
in `agent_gateway`.

On each terminal event the sink resolves the coding-agent plugin from the
run row's `plugin_id`, calls `plugin.parse_result(outputs)` to derive a
`RunResult`, derives `RunStatus` from the wire `event_kind`, and persists
via `finalize_run`. Returns `{"output": result.output, "error_message":
result.error_message}` so agent_gateway can merge those keys into the
run outputs.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import AgentEventEnrichment
from app.core.coding_agent.run_service import finalize_run, get_run_ref_for_command
from app.core.coding_agent.service import PluginNotFoundError, get_plugin

log = structlog.get_logger("core.coding_agent.run_sink")

# Only this command kind produces run rows.
_INVOKE_CLAUDE_CODE_KIND = "InvokeClaudeCode"


class CodingAgentRunSinkImpl:
    """Finalizes `coding_agent_runs` rows on `InvokeClaudeCode` terminal events.

    Also persists one `coding_agent_activity` row per run with the
    pre-rendered `ActivityLog` blob.
    """

    async def handle_terminal_event(
        self,
        command_id: UUID,
        command_kind: str,
        event_kind: str,
        outputs: dict,  # type: ignore[type-arg]
        session: AsyncSession,
    ) -> AgentEventEnrichment | None:
        """Finalize the run row + persist the activity blob for this terminal event.

        No-ops silently for all other command kinds — provision/cleanup/
        writefiles/refreshauth do not have run rows.

        `outputs` is the full AgentEvent outputs dict from the agent; it is
        passed directly to `plugin.parse_result` which reads `stdout` and
        `exit_code` from it. `parse_result` extracts the structured response
        JSON from the stream-json `result` field and places it in `RunResult.output`.

        Returns an `AgentEventEnrichment` on `InvokeClaudeCode` terminal events
        so `agent_gateway` can merge those keys into the run outputs.
        Returns `None` for all other command kinds.
        """
        if command_kind != _INVOKE_CLAUDE_CODE_KIND:
            return None

        run_ref = await get_run_ref_for_command(command_id, session=session)
        if run_ref is None:
            # No run row exists for this command — log and skip.
            log.warning(
                "coding_agent.run_sink.no_run_row",
                command_id=str(command_id),
                command_kind=command_kind,
            )
            return None

        run_id = run_ref.run_id

        # Resolve the plugin from the run row's `plugin_id`. Defensive: in a
        # misconfigured or multi-plugin deployment the sink may be loaded
        # without the plugin that issued the run registered. Skip finalisation
        # (the run row stays unfinalised) rather than raising — the run
        # still proceeds off the terminal event.
        try:
            plugin = get_plugin(run_ref.plugin_id)
        except PluginNotFoundError:
            log.warning(
                "coding_agent.run_sink.plugin_not_found",
                command_id=str(command_id),
                run_id=str(run_id),
                plugin_id=run_ref.plugin_id,
            )
            return None

        # Derive status from the wire event_kind — NOT from the plugin.
        status = "success" if event_kind == "completed_success" else "failure"

        # parse_result is a pure function — never raises on missing keys;
        # a malformed payload collapses to an empty RunResult.
        result = plugin.parse_result(outputs)

        await finalize_run(
            run_id,
            usage=result.usage,
            duration_ms=result.duration_ms,
            activity=result.activity,
            exit_code=result.exit_code,
            status=status,
            session=session,
        )
        log.debug(
            "coding_agent.run.finalized_via_sink",
            run_id=str(run_id),
            command_id=str(command_id),
            status=status,
            exit_code=result.exit_code,
            tokens_in=result.usage.tokens_in,
            tokens_out=result.usage.tokens_out,
            duration_ms=result.duration_ms,
            activity_events=len(result.activity.events),
        )
        enrichment: AgentEventEnrichment = {
            "output": result.output,
            "error_message": result.error_message,
        }
        return enrichment
