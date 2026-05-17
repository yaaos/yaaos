"""Routing + formatting for `GitHubPlugin.post_review`.

A review with inline findings posts each one to `/pulls/{n}/comments`. A
finding without `file`/`line_start` falls through to `/issues/{n}/comments`,
which is GitHub's path for non-inline PR comments. The secrets-warning case
(no findings, only `summary_body`) takes the same fallback.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.domain.vcs import Finding, Review, VCSPullRequest
from app.plugins.github.service import GitHubPlugin, _format_finding_body

_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
_BASE_URL = "https://fake.github.test"


def _stub_pr(head_sha: str = "deadbeef") -> VCSPullRequest:
    return VCSPullRequest(
        plugin_id="github",
        external_id="acme/web#7",
        repo_external_id="acme/web",
        number=7,
        title="t",
        body=None,
        author_login="alice",
        author_type="user",
        base_branch="main",
        head_branch="feat",
        base_sha="aaa",
        head_sha=head_sha,
        is_draft=False,
        is_fork=False,
        state="open",
        html_url="",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def plugin(monkeypatch: pytest.MonkeyPatch) -> GitHubPlugin:
    p = GitHubPlugin()
    monkeypatch.setattr(GitHubPlugin, "base_url", _BASE_URL)

    async def _org_id(self: GitHubPlugin) -> UUID:
        return _ORG_ID

    async def _headers(self: GitHubPlugin, org_id: UUID) -> dict[str, str]:
        return {}

    async def _fetch_pr(self: GitHubPlugin, external_id: str) -> VCSPullRequest:
        return _stub_pr()

    monkeypatch.setattr(GitHubPlugin, "_resolve_org_id", _org_id)
    monkeypatch.setattr(GitHubPlugin, "_api_headers", _headers)
    monkeypatch.setattr(GitHubPlugin, "fetch_pr", _fetch_pr)
    return p


def _finding(**kw: Any) -> Finding:
    base: dict[str, Any] = {
        "severity": "must-fix",
        "title": "T",
        "body": "B",
    }
    base.update(kw)
    return Finding(**base)


async def test_inline_findings_post_one_per_finding(plugin: GitHubPlugin, httpx_mock: Any) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/repos/acme/web/pulls/7/comments",
        json={"id": 100},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/repos/acme/web/pulls/7/comments",
        json={"id": 101},
    )
    review = Review(
        agent_tag="yaaos",
        state="COMMENT",
        findings=[
            _finding(file="a.py", line_start=10, source_agent="yaaos-docs"),
            _finding(file="b.py", line_start=20, source_agent="yaaos-security"),
        ],
    )

    result = await plugin.post_review("acme/web#7", review)

    assert result.review_external_id == ""
    assert result.finding_to_comment_external_id == {0: "100", 1: "101"}
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    bodies = [json.loads(r.content) for r in requests]
    assert bodies[0]["commit_id"] == "deadbeef"
    assert bodies[0]["path"] == "a.py"
    assert bodies[0]["line"] == 10
    assert bodies[1]["path"] == "b.py"


async def test_orphan_findings_go_to_issue_comments(plugin: GitHubPlugin, httpx_mock: Any) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/repos/acme/web/pulls/7/comments",
        json={"id": 100},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/repos/acme/web/issues/7/comments",
        json={"id": 200},
    )
    review = Review(
        agent_tag="yaaos",
        state="COMMENT",
        findings=[
            _finding(file="a.py", line_start=10),
            _finding(file=None, line_start=None, title="Cross-cutting"),
        ],
    )

    result = await plugin.post_review("acme/web#7", review)

    assert result.finding_to_comment_external_id == {0: "100", 1: "200"}
    urls = [str(r.url) for r in httpx_mock.get_requests()]
    assert urls[0].endswith("/pulls/7/comments")
    assert urls[1].endswith("/issues/7/comments")


async def test_summary_body_only_routes_to_issue_comments(plugin: GitHubPlugin, httpx_mock: Any) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/repos/acme/web/issues/7/comments",
        json={"id": 200},
    )
    review = Review(
        agent_tag="yaaos",
        state="COMMENT",
        summary_body="yaaos refused — secret detected",
        findings=[],
    )

    result = await plugin.post_review("acme/web#7", review)

    assert result.finding_to_comment_external_id == {}
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["body"] == "yaaos refused — secret detected"


async def test_empty_review_does_nothing(plugin: GitHubPlugin, httpx_mock: Any) -> None:
    review = Review(agent_tag="yaaos", state="COMMENT", findings=[])

    result = await plugin.post_review("acme/web#7", review)

    assert result.finding_to_comment_external_id == {}
    assert httpx_mock.get_requests() == []


def test_format_finding_body_includes_title_body_and_agent_emoji() -> None:
    f = Finding(
        severity="must-fix",
        title="Stray test text prepended to WORKSHOP.md",
        body="Lines 1-2 add `test 4, test` above the document's H1 heading.",
        rationale="Breaks document structure.",
        source_agent="yaaos-docs",
    )

    out = _format_finding_body(f)

    assert "**Stray test text prepended to WORKSHOP.md**" in out
    assert "Lines 1-2" in out
    assert "> Breaks document structure." in out
    assert "<sub>📝 yaaos-docs</sub>" in out


def test_format_finding_body_falls_back_to_robot_emoji() -> None:
    f = Finding(
        severity="nit",
        title="t",
        body="b",
        source_agent="yaaos-unknown-agent",
    )
    assert "<sub>🤖 yaaos-unknown-agent</sub>" in _format_finding_body(f)


def test_format_finding_body_omits_agent_line_when_unset() -> None:
    f = Finding(severity="nit", title="t", body="b")
    assert "<sub>" not in _format_finding_body(f)
