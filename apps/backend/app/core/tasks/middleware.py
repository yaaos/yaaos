"""Broker middleware: enter org_context before each task body runs.

`OrgContextMiddleware` reads `TaskMetadata` from the taskiq message labels
(placed there by the drain dispatcher via `.with_labels(metadata=...)`).
When present, it enters `org_context(org_id, ActorKind.SYSTEM)` before the
task body runs and exits it cleanly after — whether the body succeeds or
raises.

Tasks enqueued outside any org context (metadata absent) run without an
org context. Handler code never enters context manually — the middleware
handles it.

Wire encoding: the drain dumps `TaskMetadata` as a JSON string before
`with_labels`, so the label arrives as a JSON string and is parsed via
`TaskMetadata.model_validate_json`. Tests that call `pre_execute`
directly with a raw dict are also supported — `model_validate` accepts
both.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from taskiq import TaskiqMessage, TaskiqMiddleware
from taskiq.result import TaskiqResult

from app.core.audit_log import ActorKind
from app.core.auth import org_context
from app.core.tasks.types import TaskMetadata


def _parse_metadata(raw: Any) -> TaskMetadata | None:
    """Return the `TaskMetadata` from a label value, or `None` if absent.

    Accepts the normal wire path (JSON string from the drain) and the
    test escape hatch (raw dict, when `pre_execute` is called directly).
    Returns `None` for missing or unparseable values.
    """
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            return TaskMetadata.model_validate_json(raw) if raw else None
        if isinstance(raw, dict):
            return TaskMetadata.model_validate(raw) if raw else None
    except ValidationError:
        return None
    return None


class OrgContextMiddleware(TaskiqMiddleware):
    """Wraps each task body in org_context when metadata carries org_id."""

    def __init__(self) -> None:
        super().__init__()
        # Keyed on `task_id` (UUID per taskiq invocation), so concurrent tasks
        # sharing this singleton middleware never collide. asyncio does not
        # reuse Task objects, so contextvar state cannot leak across tasks.
        self._active: dict[str, Any] = {}

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        metadata = _parse_metadata(message.labels.get("metadata"))
        if metadata is not None and metadata.org_id is not None:
            ctx = org_context(metadata.org_id, ActorKind.SYSTEM)
            await ctx.__aenter__()
            self._active[message.task_id] = ctx

        return message

    async def post_execute(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
    ) -> None:
        ctx = self._active.pop(message.task_id, None)
        if ctx is not None:
            await ctx.__aexit__(None, None, None)

    async def on_error(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
        exception: BaseException,
    ) -> None:
        ctx = self._active.pop(message.task_id, None)
        if ctx is not None:
            # Don't forward exception info — `org_context` is an
            # `@asynccontextmanager`, so passing it would call `gen.athrow(exc)`
            # on the generator and re-raise out of `__aexit__`. The `finally:`
            # block runs either way; we just want the contextvar reset.
            await ctx.__aexit__(None, None, None)


# Module-level singleton wired into the broker at worker boot (see runtime.py).
org_context_middleware = OrgContextMiddleware()
