"""Service test: health_check_all records exception + ERROR status on the span.

`test_health_check_all_span_records_error` — bind a stub plugin whose
`health_check` raises; assert `coding_agent.{plugin_id}.health_check`
span is `StatusCode.ERROR` with an `exception` event.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from opentelemetry.trace import StatusCode

from app.core.coding_agent import (
    bind_coding_agent_registry,
    current_coding_agent_registry,
    health_check_all,
)
from app.testing.observability import span_capture

pytestmark = pytest.mark.service


# ── Minimal stub helpers ──────────────────────────────────────────────────────


class _RaisingHealthPlugin:
    """Stub plugin whose `health_check` raises; all other methods unused here."""

    plugin_id: str = "test_health_plugin"

    async def health_check(self) -> Any:
        raise RuntimeError("simulated health check failure")

    async def review(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def incremental_review(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def verify_fix(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def stale_check(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def answer_question(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def validate_config(self, agent_config: dict[str, Any]) -> Any:
        return None


@contextmanager
def _bind_raising_health_plugin() -> Iterator[_RaisingHealthPlugin]:
    """Bind the raising plugin into a fresh registry copy; restore on exit."""
    plugin = _RaisingHealthPlugin()
    prior = current_coding_agent_registry()
    fresh = prior.copy()
    fresh.replace(plugin)  # type: ignore[arg-type]
    bind_coding_agent_registry(fresh)
    try:
        yield plugin
    finally:
        bind_coding_agent_registry(prior)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_all_span_records_error() -> None:
    """`health_check_all` opens a span; a plugin exception sets ERROR + records it."""
    with _bind_raising_health_plugin():
        with span_capture() as exporter:
            result = await health_check_all()

    # health_check_all must NOT propagate the exception — it converts to HealthStatus
    assert "test_health_plugin" in result
    assert result["test_health_plugin"].healthy is False
    assert "simulated health check failure" in result["test_health_plugin"].message

    spans = exporter.get_finished_spans()
    target = next(
        (s for s in spans if s.name == "coding_agent.test_health_plugin.health_check"),
        None,
    )
    assert target is not None, (
        f"expected 'coding_agent.test_health_plugin.health_check' span; got: {[s.name for s in spans]}"
    )

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, (
        f"expected exception event on span, got events: {[e.name for e in target.events]}"
    )
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status, got {target.status.status_code}"
    )
