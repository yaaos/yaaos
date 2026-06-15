"""Service test: `_sweep_once` propagates exceptions so the `@scheduled` task
retry mechanism sees them.

Previously this module had a forever-loop (`run_sweep_loop`) with inline
try/except + span recording. That loop is gone — sweep runs as a `@scheduled`
worker task. The task retry + error-recording is the taskiq responsibility;
`_sweep_once` deliberately lets exceptions propagate rather than swallowing them.

This test verifies the propagation contract (body raises on failure) rather
than the old loop's catch-and-continue contract.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.service


@pytest.mark.asyncio
async def test_mcp_proxy_sweep_propagates_exception_on_failure(db_session) -> None:  # type: ignore[no-untyped-def]
    """_sweep_once propagates exceptions so the `@scheduled` retry mechanism sees them."""
    import app.domain.mcp_proxy.service as _svc  # noqa: PLC0415

    original_sweep = _svc.sweep_expired

    async def _raising_sweep(*, session):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated sweep failure")

    _svc.sweep_expired = _raising_sweep  # type: ignore[attr-defined]

    try:
        with pytest.raises(RuntimeError, match="simulated sweep failure"):
            await _svc._sweep_once()
    finally:
        _svc.sweep_expired = original_sweep  # type: ignore[attr-defined]
