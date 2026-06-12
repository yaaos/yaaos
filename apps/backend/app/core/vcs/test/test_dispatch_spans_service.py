"""Service tests: VCS dispatch helpers open and record spans correctly.

Two scenarios:
- `test_vcs_dispatch_span_status_on_error` — a `post_finding` that raises
  produces a `vcs.{plugin_id}.post_finding` span with an `exception` event
  and `StatusCode.ERROR`.
- `test_httpx_outbound_call_produces_child_span` — a real `httpx.AsyncClient`
  POST inside a VCS dispatch span appears as a child HTTP span (auto-instrumented
  by `HTTPXClientInstrumentor`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from opentelemetry.trace import StatusCode

from app.core import vcs as _vcs
from app.core.vcs import (
    Comment,
    Diff,
    VCSPullRequest,
    bind_vcs_registry,
    current_vcs_registry,
)
from app.testing.observability import span_capture

pytestmark = pytest.mark.service


# ── Minimal stub helpers ──────────────────────────────────────────────────────


class _RaisingVCSPlugin:
    """Stub that raises on `post_finding`; all other methods return no-ops."""

    plugin_id: str = "test_vcs"

    def install_url(self, org_id: UUID) -> str | None:
        return None

    def validate_settings(self, settings: dict[str, object]) -> dict[str, object]:
        return {}

    def clone_url(self, repo_external_id: str) -> str:
        return f"https://example.test/{repo_external_id}.git"

    async def fetch_pr(self, org_id: UUID, external_id: str) -> VCSPullRequest:
        return VCSPullRequest(
            plugin_id=self.plugin_id,
            external_id=external_id,
            repo_external_id="owner/repo",
            number=1,
            title="stub",
            body="",
            author_login="alice",
            author_type="user",
            base_branch="main",
            head_branch="feat",
            base_sha="aaa",
            head_sha="bbb",
            is_draft=False,
            is_fork=False,
            state="open",
            html_url="https://example.test/1",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def fetch_diff(self, org_id: UUID, external_id: str) -> Diff:
        return Diff(raw="", files=[])

    async def list_yaaos_comments(self, org_id: UUID, external_id: str) -> list[Comment]:
        return []

    async def is_repo_accessible(self, org_id: UUID, repo_external_id: str) -> bool:
        return True

    async def detect_force_push(
        self, org_id: UUID, repo_external_id: str, before_sha: str, after_sha: str
    ) -> bool:
        return False

    async def list_commit_messages(
        self, org_id: UUID, repo_external_id: str, prev_sha: str, head_sha: str
    ) -> list[str]:
        return []

    async def post_finding(
        self,
        org_id: UUID,
        external_id: str,
        *,
        file: str | None,
        line_start: int | None,
        line_end: int | None,
        severity: str,
        category: str,
        confidence: str,
        finding_display_id: int,
        rationale: str,
        rule_violated: str,
        rule_source: str,
        suggested_fix: str | None,
    ) -> str:
        raise RuntimeError("simulated vcs post_finding failure")

    async def post_comment(self, org_id: UUID, external_id: str, *, body: str) -> str:
        return "stub-comment-id"

    async def post_comment_reply(
        self, org_id: UUID, external_id: str, parent_comment_external_id: str, body: str
    ) -> str:
        return "stub-reply-id"

    async def mark_comments_outdated(
        self, org_id: UUID, external_id: str, comment_external_ids: list[str]
    ) -> None:
        pass

    async def get_installation_token(self, org_id: UUID) -> str:
        return "stub-token"

    async def list_installation_repos(self, org_id: UUID) -> list[str]:
        return []


@contextmanager
def _bind_raising_plugin() -> Iterator[_RaisingVCSPlugin]:
    """Bind a raising-plugin copy into the current registry; restore on exit."""
    plugin = _RaisingVCSPlugin()
    prior = current_vcs_registry()
    fresh = prior.copy()
    fresh.replace(plugin)  # type: ignore[arg-type]
    bind_vcs_registry(fresh)
    try:
        yield plugin
    finally:
        bind_vcs_registry(prior)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vcs_dispatch_span_status_on_error() -> None:
    """`vcs.post_finding` dispatch opens a span; a plugin exception sets ERROR + records it."""
    org_id = uuid.uuid4()

    with _bind_raising_plugin():
        with span_capture() as exporter:
            try:
                await _vcs.post_finding(
                    "test_vcs",
                    org_id,
                    "owner/repo#1",
                    file=None,
                    line_start=None,
                    line_end=None,
                    severity="high",
                    category="security",
                    confidence="high",
                    finding_display_id=1,
                    rationale="test rationale",
                    rule_violated="rule",
                    rule_source="source",
                    suggested_fix=None,
                )
            except RuntimeError:
                pass  # expected — the span must have been recorded

    spans = exporter.get_finished_spans()
    target = next(
        (s for s in spans if s.name == "vcs.test_vcs.post_finding"),
        None,
    )
    assert target is not None, f"expected 'vcs.test_vcs.post_finding' span; got: {[s.name for s in spans]}"

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, (
        f"expected exception event on span, got events: {[e.name for e in target.events]}"
    )
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status, got {target.status.status_code}"
    )


@pytest.mark.asyncio
async def test_httpx_outbound_call_produces_child_span(httpx_mock) -> None:  # type: ignore[no-untyped-def]
    """HTTPXClientInstrumentor auto-spans each httpx request as a child of the VCS dispatch span.

    Uses pytest-httpx (`httpx_mock`) to intercept the outgoing request.

    The `HTTPXClientInstrumentor` wraps `AsyncHTTPTransport.handle_async_request`.
    The `pytest_httpx` fixture also wraps that method. Order matters: we
    (re-)install the OTel instrumentation AFTER the pytest_httpx fixture has set up
    so the call chain is: OTel-wrapper → pytest_httpx-wrapper → mock response.
    This guarantees the OTel HTTP span is opened even when the request is intercepted.
    """
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415

    org_id = uuid.uuid4()

    # A plugin whose post_comment issues a real httpx request intercepted by httpx_mock.
    class _HttpxPlugin(_RaisingVCSPlugin):
        plugin_id: str = "test_httpx_vcs"

        async def post_comment(self, org_id: UUID, external_id: str, *, body: str) -> str:
            async with httpx.AsyncClient() as client:
                resp = await client.post("https://stub.test/comments", json={"body": body})
                resp.raise_for_status()
            return str(resp.json().get("id", "httpx-stub-id"))

    plugin = _HttpxPlugin()
    prior = current_vcs_registry()
    fresh = prior.copy()
    fresh.replace(plugin)  # type: ignore[arg-type]
    bind_vcs_registry(fresh)

    httpx_mock.add_response(url="https://stub.test/comments", method="POST", json={"id": "c1"})

    # (Re-)install the OTel instrumentation so it sits on top of whatever
    # pytest_httpx put on AsyncHTTPTransport.handle_async_request. This ensures
    # the call chain is: OTel span → pytest_httpx intercept → mock response.
    # If already instrumented from a prior configure() call, we must first
    # uninstrument (to remove it from under pytest_httpx) then re-instrument
    # (to place it above pytest_httpx). We restore the prior state on exit.
    instrumentor = HTTPXClientInstrumentor()
    was_instrumented = instrumentor.is_instrumented_by_opentelemetry
    if was_instrumented:
        instrumentor.uninstrument()
    instrumentor.instrument()
    try:
        with span_capture() as exporter:
            await _vcs.post_comment(
                "test_httpx_vcs",
                org_id,
                "owner/repo#1",
                body="hello",
            )
    finally:
        instrumentor.uninstrument()
        if was_instrumented:
            # Restore global instrumentation that was active before this test.
            instrumentor.instrument()
        bind_vcs_registry(prior)

    spans = exporter.get_finished_spans()
    span_names = [s.name for s in spans]

    vcs_span = next(
        (s for s in spans if s.name == "vcs.test_httpx_vcs.post_comment"),
        None,
    )
    assert vcs_span is not None, f"expected 'vcs.test_httpx_vcs.post_comment' span; got: {span_names}"

    # The HTTPXClientInstrumentor produces a child HTTP span; its name is
    # "POST" or "HTTP POST" depending on the instrumentor version.
    http_spans = [
        s
        for s in spans
        if "POST" in s.name.upper() and s.parent is not None and s.parent.span_id == vcs_span.context.span_id  # type: ignore[union-attr]
    ]
    assert http_spans, (
        f"expected an HTTP child span under vcs.test_httpx_vcs.post_comment; all spans: {span_names}"
    )
