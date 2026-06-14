"""Service test: failure-shaped catches in core/identity/scheduler record exception events on spans.

Forces run_identity_purge to raise via a patched purge helper and asserts
the active span carries an `exception` event.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.testing.observability import span_capture

pytestmark = pytest.mark.service


@pytest.mark.asyncio
async def test_identity_purge_failure_records_on_span() -> None:
    """run_identity_purge failure records exception + ERROR on the active span."""
    import app.core.identity.scheduler as _sched  # noqa: PLC0415

    original = _sched._purge_expired_sessions

    async def _raising_purge() -> int:
        raise RuntimeError("simulated purge failure")

    _sched._purge_expired_sessions = _raising_purge  # type: ignore[attr-defined]

    try:
        with span_capture() as exporter:
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("spawn:identity_purge"):
                try:
                    await _sched.run_identity_purge()
                except RuntimeError:
                    pass  # the re-raise is expected
    finally:
        _sched._purge_expired_sessions = original  # type: ignore[attr-defined]

    spans = exporter.get_finished_spans()
    target = next((s for s in spans if "identity_purge" in s.name), None)
    assert target is not None, f"no identity_purge span; got: {[s.name for s in spans]}"

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, f"expected exception event on span, got: {[e.name for e in target.events]}"
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status, got {target.status.status_code}"
    )
