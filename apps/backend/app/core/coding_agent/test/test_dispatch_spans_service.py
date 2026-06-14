"""Service test: coding-agent dispatch helpers open and record spans correctly.

`test_coding_agent_dispatch_span_records_error` — bind a stub plugin whose
`review` raises a `CodingAgentError`; assert `coding_agent.{plugin_id}.review`
span is `StatusCode.ERROR` with an `exception` event.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from opentelemetry.trace import StatusCode

from app.core.coding_agent import (
    CodingAgentError,
    OnActivity,
    ReviewContext,
    ReviewResult,
    bind_coding_agent_registry,
    current_coding_agent_registry,
    review,
)
from app.testing.observability import span_capture

pytestmark = pytest.mark.service


# ── Minimal stub helpers ──────────────────────────────────────────────────────


class _RaisingPlugin:
    """Stub plugin whose `review` raises `CodingAgentError`; all other methods
    are not exercised by this test."""

    plugin_id: str = "test_coding_agent"

    async def review(
        self,
        workspace: Any,
        context: ReviewContext,
        on_activity: OnActivity | None = None,
    ) -> ReviewResult:
        raise CodingAgentError("simulated review failure")

    async def validate_config(self, agent_config: dict[str, Any]) -> Any:
        return None

    async def health_check(self) -> Any:
        return None


@contextmanager
def _bind_raising_plugin() -> Iterator[_RaisingPlugin]:
    """Bind the raising plugin into a fresh registry copy; restore on exit."""
    plugin = _RaisingPlugin()
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
async def test_coding_agent_dispatch_span_records_error() -> None:
    """`review` dispatch opens a span; a plugin exception sets ERROR + records it."""
    from uuid import UUID  # noqa: PLC0415

    ctx = ReviewContext(
        org_id=UUID(int=1),
        repo_external_id="acme/repo",
        pr_external_id="acme/repo#1",
        head_sha="head",
        base_sha="base",
    )

    with _bind_raising_plugin():
        with span_capture() as exporter:
            try:
                await review("test_coding_agent", workspace=None, context=ctx)  # type: ignore[arg-type]
            except CodingAgentError:
                pass  # expected — the span must have been recorded

    spans = exporter.get_finished_spans()
    target = next(
        (s for s in spans if s.name == "coding_agent.test_coding_agent.review"),
        None,
    )
    assert target is not None, (
        f"expected 'coding_agent.test_coding_agent.review' span; got: {[s.name for s in spans]}"
    )

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, (
        f"expected exception event on span, got events: {[e.name for e in target.events]}"
    )
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status, got {target.status.status_code}"
    )
