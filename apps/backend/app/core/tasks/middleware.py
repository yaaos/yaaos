"""Broker middleware: enter org_context before each task body runs.

`OrgContextMiddleware` reads `metadata["org_id"]` from the taskiq message
labels (placed there by the drain dispatcher via `.with_labels(metadata=...)`).
When present, it enters `org_context(org_id, ActorKind.SYSTEM)` before the
task body runs and exits it cleanly after — whether the body succeeds or
raises.

Tasks enqueued outside any org context (metadata absent or org_id missing)
run without an org context. Handler code never enters context manually —
the middleware handles it.

Label encoding note: taskiq's label serialization coerces non-primitive values
to `str(value)` (Python repr). `metadata` in labels arrives as a Python repr
string such as `"{'org_id': '...'}"`. `_parse_metadata` recovers the dict
safely via `ast.literal_eval` — the values are always UUID strings so there
is no security risk.
"""

from __future__ import annotations

import ast
from typing import Any
from uuid import UUID

from taskiq import TaskiqMessage, TaskiqMiddleware
from taskiq.result import TaskiqResult

from app.core.audit_log import ActorKind
from app.core.auth import org_context


def _parse_metadata(raw: Any) -> dict[str, Any] | None:
    """Return the metadata dict from a label value.

    When the value is already a dict (e.g. in tests that call pre_execute
    directly), return as-is. When it is a string (the normal wire path —
    taskiq coerces non-primitive label values via `str()`), parse it with
    `ast.literal_eval` so the dict is recovered. Returns `None` when the
    value is absent, an empty dict, or not parseable.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw or None
    if isinstance(raw, str) and raw:
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, dict):
                return parsed or None
        except (ValueError, SyntaxError):
            pass
    return None


class OrgContextMiddleware(TaskiqMiddleware):
    """Wraps each task body in org_context when metadata carries org_id."""

    def __init__(self) -> None:
        super().__init__()
        # Stores the active context manager per task_id so pre/post hooks pair up.
        self._active: dict[str, Any] = {}

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        metadata = _parse_metadata(message.labels.get("metadata"))
        org_id_str: str | None = metadata.get("org_id") if metadata else None

        if org_id_str:
            ctx = org_context(UUID(org_id_str), ActorKind.SYSTEM)
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
            await ctx.__aexit__(type(exception), exception, exception.__traceback__)


# Module-level singleton wired into the broker at worker boot (see runtime.py).
org_context_middleware = OrgContextMiddleware()
