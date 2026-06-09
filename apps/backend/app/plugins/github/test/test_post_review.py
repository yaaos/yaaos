"""Routing + formatting for `GitHubPlugin.post_finding` and `post_comment`.

A finding with `file`/`line_start` posts to `/pulls/{n}/comments`. A
finding without `file`/`line_start` (null-anchor) falls through to
`/issues/{n}/comments`, which is GitHub's path for non-inline PR comments.
`post_comment` always posts to the issue-comments endpoint.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.core.vcs import VCSAuthError, VCSPullRequest
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


def _finding_kwargs(
    *,
    file: str | None = "a.py",
    line_start: int | None = 10,
    line_end: int | None = 10,
    severity: str = "blocker",
    category: str = "security",
    confidence: str = "verified",
    finding_display_id: int = 1,
    rationale: str = "Reason.",
    rule_violated: str = "rule-x",
    rule_source: str = "owasp",
    suggested_fix: str | None = "Fix it.",
) -> dict:
    return dict(
        file=file,
        line_start=line_start,
        line_end=line_end,
        severity=severity,
        category=category,
        confidence=confidence,
        finding_display_id=finding_display_id,
        rationale=rationale,
        rule_violated=rule_violated,
        rule_source=rule_source,
        suggested_fix=suggested_fix,
    )


async def test_inline_finding_posts_to_pulls_comments(plugin: GitHubPlugin, httpx_mock) -> None:  # type: ignore[no-untyped-def]
    """Anchored finding (file + line) posts to the pull-request inline endpoint."""
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/repos/acme/web/pulls/7/comments",
        json={"id": 100},
    )

    comment_id = await plugin.post_finding("acme/web#7", **_finding_kwargs())

    assert comment_id == "100"
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["commit_id"] == "deadbeef"
    assert body["path"] == "a.py"
    assert body["line"] == 10


async def test_null_anchor_finding_posts_to_issue_comments(plugin: GitHubPlugin, httpx_mock) -> None:  # type: ignore[no-untyped-def]
    """Finding with no file/line routes to the issue-comments endpoint (top-level PR comment)."""
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/repos/acme/web/issues/7/comments",
        json={"id": 200},
    )

    comment_id = await plugin.post_finding(
        "acme/web#7", **_finding_kwargs(file=None, line_start=None, line_end=None)
    )

    assert comment_id == "200"
    urls = [str(r.url) for r in httpx_mock.get_requests()]
    assert urls[0].endswith("/issues/7/comments")


async def test_post_comment_posts_to_issue_comments(plugin: GitHubPlugin, httpx_mock) -> None:  # type: ignore[no-untyped-def]
    """`post_comment` always routes to the issue-comments endpoint."""
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE_URL}/repos/acme/web/issues/7/comments",
        json={"id": 300},
    )

    comment_id = await plugin.post_comment("acme/web#7", body="yaaos refused — secrets detected")

    assert comment_id == "300"
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["body"] == "yaaos refused — secrets detected"


async def test_list_installation_repos_returns_full_names(
    plugin: GitHubPlugin, monkeypatch: pytest.MonkeyPatch, httpx_mock
) -> None:  # type: ignore[no-untyped-def]
    """Maps `/installation/repositories` to a flat list of repo full-names."""

    async def _token(self: GitHubPlugin, org_id: UUID) -> str:
        return "install-tok"

    monkeypatch.setattr(GitHubPlugin, "_installation_token", _token)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_URL}/installation/repositories?per_page=100",
        json={"total_count": 2, "repositories": [{"full_name": "acme/web"}, {"full_name": "acme/api"}]},
    )

    repos = await plugin.list_installation_repos(_ORG_ID)

    assert repos == ["acme/web", "acme/api"]


async def test_list_installation_repos_empty_on_error(
    plugin: GitHubPlugin, monkeypatch: pytest.MonkeyPatch, httpx_mock
) -> None:  # type: ignore[no-untyped-def]
    """A non-200 from GitHub yields an empty list, not an exception."""

    async def _token(self: GitHubPlugin, org_id: UUID) -> str:
        return "install-tok"

    monkeypatch.setattr(GitHubPlugin, "_installation_token", _token)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_URL}/installation/repositories?per_page=100",
        status_code=404,
        json={},
    )

    assert await plugin.list_installation_repos(_ORG_ID) == []


async def test_list_installation_repos_empty_on_missing_install(
    plugin: GitHubPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No active install (token raises) yields an empty list."""

    async def _token(self: GitHubPlugin, org_id: UUID) -> str:
        raise VCSAuthError("no active GitHub App installation")

    monkeypatch.setattr(GitHubPlugin, "_installation_token", _token)

    assert await plugin.list_installation_repos(_ORG_ID) == []


def test_format_finding_body_includes_handle_and_rule() -> None:
    out = _format_finding_body(
        finding_display_id=1,
        category="security",
        severity="blocker",
        confidence="verified",
        rationale="Unvalidated input passed to SQL query.",
        rule_violated="sql-injection",
        rule_source="owasp",
        suggested_fix="Use parameterized queries.",
    )

    assert "[sec-1]" in out
    assert "sql-injection" in out
    assert "Unvalidated input passed to SQL query." in out
    assert "blocker" in out
    assert "verified" in out
    assert "Use parameterized queries." in out


def test_format_finding_body_omits_suggested_fix_when_none() -> None:
    out = _format_finding_body(
        finding_display_id=2,
        category="correctness",
        severity="nit",
        confidence="speculative",
        rationale="Minor naming issue.",
        rule_violated="naming",
        rule_source="house",
        suggested_fix=None,
    )

    assert "Suggested fix" not in out
    assert "Minor naming issue." in out
