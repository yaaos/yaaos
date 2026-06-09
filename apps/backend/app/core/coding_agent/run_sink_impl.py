"""Concrete implementation of `AgentRunSink`.

Registered by `core/coding_agent.__init__` at import time.
Fires only on `InvokeClaudeCode` terminal events — all other command kinds
are silently no-ops so the sink can be registered without per-kind checks
in `agent_gateway`.

On each terminal event the sink resolves the coding-agent plugin from the
run row's `plugin_id`, runs `parse_usage(stdout)` + `render_activity(stdout)`
to derive the real `Usage` + `ActivityLog`, and passes them to `finalize_run`
which writes both the run row and the activity blob in the caller's
transaction.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.coding_agent.run_service import finalize_run, get_run_ref_for_command
from app.core.coding_agent.service import PluginNotFoundError, get_plugin
from app.core.coding_agent.types import ActivityLog, Usage

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
    ) -> None:
        """Finalize the run row + persist the activity blob for this terminal event.

        No-ops silently for all other command kinds — provision/cleanup/
        writefiles/refreshauth do not have run rows.

        `outputs` carries `exit_code` (int | None) and `stdout` (str | None)
        from `AgentEvent.outputs` (set by `apps/agent/internal/command/results.go`).
        """
        if command_kind != _INVOKE_CLAUDE_CODE_KIND:
            return

        run_ref = await get_run_ref_for_command(command_id, session=session)
        if run_ref is None:
            # No run row exists for this command — log and skip.
            log.warning(
                "coding_agent.run_sink.no_run_row",
                command_id=str(command_id),
                command_kind=command_kind,
            )
            return

        run_id = run_ref.run_id

        # Resolve the plugin from the run row's `plugin_id`. Defensive: in a
        # misconfigured or multi-plugin deployment the sink may be loaded
        # without the plugin that issued the run registered. Skip finalisation
        # (the run row stays unfinalised) rather than raising — the workflow
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
            return

        status = "success" if event_kind == "completed_success" else "failure"
        exit_code_raw = outputs.get("exit_code")
        exit_code: int | None = int(exit_code_raw) if exit_code_raw is not None else None
        stdout_raw = outputs.get("stdout") or ""

        # Resolve usage + activity from the terminal stdout via the plugin.
        # A malformed stream collapses to empty `Usage()` / empty `ActivityLog`
        # — never raises; the run row still finalizes.
        usage: Usage = plugin.parse_usage(stdout_raw)
        activity: ActivityLog = plugin.render_activity(stdout_raw)

        await finalize_run(
            run_id,
            usage=usage,
            activity=activity,
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
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            activity_events=len(activity.events),
        )
