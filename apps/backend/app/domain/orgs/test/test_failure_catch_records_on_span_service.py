"""Service test: failure-shaped catches in domain/orgs record exception events on spans.

Samples run_assertion_verifier with a verifier that raises — asserts the
span wrapping the call carries an `exception` event.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.domain.orgs.sso import register_assertion_verifier, run_assertion_verifier
from app.testing.observability import span_capture

pytestmark = pytest.mark.service


def _raising_verifier(saml_response: str, idp_metadata_xml: str) -> dict | None:
    raise RuntimeError("simulated verifier failure")


@pytest.mark.asyncio
async def test_orgs_failure_catch_records_on_span() -> None:
    """run_assertion_verifier with a raising verifier records exception + ERROR on span."""
    # Register the raising verifier; it will be picked up alongside any defaults.
    register_assertion_verifier(_raising_verifier)

    try:
        with span_capture() as exporter:
            tracer = trace.get_tracer(__name__)
            # Simulate the FastAPI SSO callback span context.
            with tracer.start_as_current_span("POST /api/sso/callback"):
                run_assertion_verifier("saml-data", "idp-meta")
    finally:
        # Remove the registered raising verifier to avoid contaminating other tests.
        from app.domain.orgs import sso as _sso  # noqa: PLC0415

        _sso._verifiers[:] = [v for v in _sso._verifiers if v is not _raising_verifier]

    # Result may be None (no verifier returned a non-None) — that's fine.
    # What matters is the span carries the exception event.
    spans = exporter.get_finished_spans()
    target = next((s for s in spans if "sso" in s.name.lower() or "POST" in s.name), None)
    assert target is not None, f"no span found; got: {[s.name for s in spans]}"

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, f"expected exception event on span, got: {[e.name for e in target.events]}"
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status, got {target.status.status_code}"
    )
