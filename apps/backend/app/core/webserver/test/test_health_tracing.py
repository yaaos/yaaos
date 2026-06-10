"""`/api/health` is excluded from FastAPI HTTP tracing.

Health probes (Fly's machine checker) hit `/api/health` every few seconds;
tracing each one floods the trace backend. `TRACE_EXCLUDED_URLS` is the
`excluded_urls` value passed to the FastAPI instrumentor — this guards that the
value actually disables the health path (and only it), parsed the same way the
instrumentor parses it.
"""

from __future__ import annotations

from opentelemetry.util.http import parse_excluded_urls

from app.core.observability import TRACE_EXCLUDED_URLS


def test_health_path_is_excluded() -> None:
    excluded = parse_excluded_urls(TRACE_EXCLUDED_URLS)
    assert excluded.url_disabled("/api/health")


def test_non_health_paths_are_not_excluded() -> None:
    excluded = parse_excluded_urls(TRACE_EXCLUDED_URLS)
    assert not excluded.url_disabled("/api/tickets")
    assert not excluded.url_disabled("/api/auth/me")
