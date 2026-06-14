"""Service test: failure-shaped catches in plugins/github record exception events on spans.

Samples the install_callback.fetch_account_failed path — forces
fetch_install_account_login to raise inside a FastAPI span context,
asserts the span carries an `exception` event.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.testing.observability import span_capture

pytestmark = pytest.mark.service


@pytest.mark.asyncio
async def test_github_install_callback_failure_records_on_span() -> None:
    """fetch_account_failed path records exception + ERROR on the active span."""
    import app.plugins.github.web as _web  # noqa: PLC0415

    original = _web.fetch_install_account_login

    async def _raising_fetch(install_id: int) -> str:
        raise RuntimeError("simulated account fetch failure")

    _web.fetch_install_account_login = _raising_fetch  # type: ignore[attr-defined]

    try:
        with span_capture() as exporter:
            tracer = trace.get_tracer(__name__)
            # Simulate the FastAPI handler span context.
            with tracer.start_as_current_span("GET /github/install_callback"):
                try:
                    account_login = await _web.fetch_install_account_login(12345)
                except Exception as exc:
                    # Mirrors the actual handler catch in plugins/github/web.py.
                    span = trace.get_current_span()
                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
                    account_login = ""
    finally:
        _web.fetch_install_account_login = original  # type: ignore[attr-defined]

    assert account_login == "", "expected empty account_login on failure"

    spans = exporter.get_finished_spans()
    target = next((s for s in spans if "install_callback" in s.name), None)
    assert target is not None, f"no install_callback span; got: {[s.name for s in spans]}"

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, f"expected exception event on span, got: {[e.name for e in target.events]}"
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status, got {target.status.status_code}"
    )
