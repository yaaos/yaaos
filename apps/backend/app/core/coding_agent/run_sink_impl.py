"""Concrete implementation of `AgentRunSink`.

Registered by `core/coding_agent.__init__` at import time.
`handle_terminal_event` fires on `InvokeClaudeCode` and `InvokeCodex`
terminal events — all other command kinds are silently no-ops so the sink
can be registered without per-kind checks in `agent_gateway`.

On each terminal event the sink resolves the coding-agent plugin from the
run row's `plugin_id` (not from the command kind), calls
`plugin.parse_result(outputs)` to derive a `RunResult`, derives `RunStatus`
from the wire `event_kind`, and persists via `finalize_run`. Returns
`{"output": result.output, "error_message": result.error_message}` so
agent_gateway can merge those keys into the run outputs.

`handle_progress_event` fires on every non-terminal `progress` AgentEvent
correlated to an `InvokeClaudeCode` or `InvokeCodex` run: resolves the
plugin the same way, extracts the raw stream line from
`outputs["stream_line"]`, maps it via `plugin.parse_activity_line`, and
publishes the normalized frame to the workspace-activity SSE channel. This
is the one place a live frame's shape is decided — the channel carries the
same `{kind, ts, message, detail}` vocabulary regardless of which plugin
produced it.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import AgentEvent, AgentEventEnrichment
from app.core.coding_agent.run_service import finalize_run, get_run_ref_for_command
from app.core.coding_agent.service import PluginNotFoundError, get_plugin
from app.core.database import session as db_session
from app.core.sse import publish_workspace_activity

log = structlog.get_logger("core.coding_agent.run_sink")

# Command kinds that produce coding-agent run rows. Both InvokeClaudeCode and
# InvokeCodex route through the same run lifecycle — the plugin resolves from
# the run row's plugin_id, not from the command kind.
_INVOKE_KINDS: frozenset[str] = frozenset({"InvokeClaudeCode", "InvokeCodex"})


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
        if command_kind not in _INVOKE_KINDS:
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

    async def handle_progress_event(
        self,
        *,
        org_id: UUID,
        run_id: UUID,
        event: AgentEvent,
    ) -> None:
        """Normalize + publish one live `progress` AgentEvent.

        Resolves the plugin via the same `get_run_ref_for_command` lookup
        `handle_terminal_event` uses (opens its own read-only session — this
        is a pure read, no writes, no composition with a caller's transaction).
        No-ops silently when: no run row exists for this command (not
        `InvokeClaudeCode`, or no run yet); the plugin can't be resolved;
        `outputs["stream_line"]` is missing or not a string; or
        `plugin.parse_activity_line` returns `None` (line has no useful render).
        """
        async with db_session() as s:
            run_ref = await get_run_ref_for_command(event.command_id, session=s)
        if run_ref is None:
            return

        try:
            plugin = get_plugin(run_ref.plugin_id)
        except PluginNotFoundError:
            log.warning(
                "coding_agent.run_sink.progress_plugin_not_found",
                command_id=str(event.command_id),
                plugin_id=run_ref.plugin_id,
            )
            return

        stream_line = event.outputs.get("stream_line")
        if not isinstance(stream_line, str):
            return

        rendered = plugin.parse_activity_line(stream_line)
        if rendered is None:
            return

        await publish_workspace_activity(
            org_id=org_id,
            run_id=run_id,
            payload=rendered.model_dump(mode="json"),
        )
