"""Service test: failure-shaped catches in domain/mcp_proxy record exception events on spans.

Forces run_sweep_loop's `sweep_expired` to raise and cancels after one
iteration; asserts the active span carries an `exception` event.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.testing.observability import span_capture

pytestmark = pytest.mark.service


@pytest.mark.asyncio
async def test_mcp_proxy_sweep_failure_records_on_span(db_session) -> None:  # type: ignore[no-untyped-def]
    """Sweep failure inside run_sweep_loop records exception + ERROR on the active span."""
    import app.domain.mcp_proxy.service as _svc  # noqa: PLC0415

    calls: list[str] = []
    original_sweep = _svc.sweep_expired

    async def _raising_sweep(*, session):  # type: ignore[no-untyped-def]
        calls.append("raised")
        raise RuntimeError("simulated sweep failure")

    _svc.sweep_expired = _raising_sweep  # type: ignore[attr-defined]

    try:
        with span_capture() as exporter:
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("spawn:mcp_proxy.sweep"):
                task = asyncio.create_task(_svc.run_sweep_loop())
                # Let one iteration run (the sleep interval is 1s in tests).
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    finally:
        _svc.sweep_expired = original_sweep  # type: ignore[attr-defined]

    assert calls, "sweep_expired was never called"

    spans = exporter.get_finished_spans()
    target = next((s for s in spans if "mcp_proxy.sweep" in s.name), None)
    assert target is not None, f"no mcp_proxy.sweep span; got: {[s.name for s in spans]}"

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, f"expected exception event on span, got: {[e.name for e in target.events]}"
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status, got {target.status.status_code}"
    )
