"""Webhook payload → VCSEvent translation."""

from app.core.vcs import (
    PullRequestClosed,
    PullRequestReadyForReview,
    PullRequestSynchronized,
)
from app.plugins.github.payload_parser import parse_webhook


def _pr_payload(action: str, **overrides) -> dict:
    pr = {
        "number": 7,
        "title": "T",
        "body": "B",
        "draft": False,
        "merged": False,
        "state": "open",
        "html_url": "https://github.com/acme/web/pull/7",
        "user": {"login": "alice", "type": "User"},
        "head": {"ref": "feat", "sha": "ccc", "repo": {"fork": False}},
        "base": {"ref": "main", "sha": "aaa"},
        "created_at": "2026-05-01T10:00:00Z",
        "updated_at": "2026-05-01T10:00:00Z",
    }
    pr.update(overrides)
    return {"action": action, "pull_request": pr, "repository": {"full_name": "acme/web"}}


def test_opened_emits_ready_for_review() -> None:
    events = parse_webhook("pull_request", "evt-1", _pr_payload("opened"))
    assert len(events) == 1
    assert isinstance(events[0], PullRequestReadyForReview)


def test_opened_draft_emits_nothing() -> None:
    events = parse_webhook("pull_request", "evt-2", _pr_payload("opened", draft=True))
    assert events == []


def test_synchronize_emits_synchronized() -> None:
    events = parse_webhook("pull_request", "evt-3", _pr_payload("synchronize"))
    assert isinstance(events[0], PullRequestSynchronized)
    assert events[0].new_head_sha == "ccc"


def test_closed_merged() -> None:
    events = parse_webhook("pull_request", "evt-4", _pr_payload("closed", merged=True))
    assert isinstance(events[0], PullRequestClosed)
    assert events[0].merged is True


def test_unknown_event_returns_empty() -> None:
    assert parse_webhook("workflow_run", "evt-5", {"action": "completed"}) == []
