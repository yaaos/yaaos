"""The discriminated Union must round-trip JSON correctly."""

from datetime import UTC, datetime

from pydantic import TypeAdapter

from app.core.vcs import (
    PullRequestClosed,
    PullRequestReadyForReview,
    VCSEvent,
    VCSPullRequest,
)

_event_adapter = TypeAdapter(VCSEvent)


def _pr() -> VCSPullRequest:
    return VCSPullRequest(
        plugin_id="github",
        external_id="acme/web#1",
        repo_external_id="acme/web",
        number=1,
        title="t",
        body="b",
        author_login="alice",
        author_type="user",
        base_branch="main",
        head_branch="feat",
        base_sha="aaa",
        head_sha="bbb",
        is_draft=False,
        is_fork=False,
        state="open",
        html_url="https://github.com/acme/web/pull/1",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_pr_ready_round_trips() -> None:
    e = PullRequestReadyForReview(
        plugin_id="github",
        source_event_id="evt-1",
        received_at=datetime.now(UTC),
        repo_external_id="acme/web",
        pr_external_id="acme/web#1",
        pr=_pr(),
    )
    j = e.model_dump(mode="json")
    parsed = _event_adapter.validate_python(j)
    assert parsed.kind == "pr_ready_for_review"


def test_pr_closed_round_trips() -> None:
    e = PullRequestClosed(
        plugin_id="github",
        source_event_id="evt-2",
        received_at=datetime.now(UTC),
        repo_external_id="acme/web",
        pr_external_id="acme/web#1",
        merged=True,
    )
    parsed = _event_adapter.validate_python(e.model_dump(mode="json"))
    assert parsed.kind == "pr_closed"
    assert parsed.merged is True  # type: ignore[union-attr]
