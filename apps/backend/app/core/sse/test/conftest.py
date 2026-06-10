"""Test-local fixtures for app/core/sse/test/."""

from __future__ import annotations

import pytest

from app.core.sse.web import _reset_shutdown_event_for_tests


@pytest.fixture(autouse=True)
def reset_sse_shutdown_event() -> None:
    """Reset the SSE shutdown event before every test in this directory.

    The shutdown event is a module-level singleton; once set it stays set
    for subsequent tests unless explicitly cleared.  This autouse fixture
    calls `_reset_shutdown_event_for_tests()` before each test so the event
    starts unset and bound to the current event loop — preventing a test that
    calls `shutdown()` from leaking a stale set-event into the next test.
    """
    _reset_shutdown_event_for_tests()
