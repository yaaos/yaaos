"""Translate GitHub webhook payloads to core/vcs VCSEvent instances."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.vcs import (
    CommentCreated,
    PullRequestClosed,
    PullRequestReadyForReview,
    PullRequestReopened,
    PullRequestSynchronized,
    ReactionAdded,
    VCSEvent,
    VCSPullRequest,
)


def _parse_pr(payload: dict[str, Any]) -> VCSPullRequest:
    pr = payload["pull_request"]
    repo = payload["repository"]
    user = pr.get("user", {}) or {}
    head = pr.get("head", {}) or {}
    base = pr.get("base", {}) or {}
    return VCSPullRequest(
        plugin_id="github",
        external_id=f"{repo['full_name']}#{pr['number']}",
        repo_external_id=repo["full_name"],
        number=pr["number"],
        title=pr.get("title", ""),
        body=pr.get("body"),
        author_login=user.get("login", "unknown"),
        author_type="bot" if user.get("type", "User").lower() == "bot" else "user",
        base_branch=base.get("ref", ""),
        head_branch=head.get("ref", ""),
        base_sha=base.get("sha", ""),
        head_sha=head.get("sha", ""),
        is_draft=pr.get("draft", False),
        # "Is this a cross-fork PR?" — contributed from a fork the install
        # doesn't own. The check is head_repo != base_repo, NOT `head.repo.fork`:
        # the latter is true whenever the head repo is itself a fork of some
        # upstream (e.g. the user's fork of an OSS project they're testing
        # yaaos against), which has nothing to do with PR provenance. The
        # github intake type's fork filter uses this signal to skip
        # external-contributor PRs, so the semantics must be correct.
        is_fork=((head.get("repo") or {}).get("full_name") != (base.get("repo") or {}).get("full_name")),
        state="merged" if pr.get("merged") else pr.get("state", "open"),
        html_url=pr.get("html_url", ""),
        created_at=_parse_iso(pr.get("created_at")),
        updated_at=_parse_iso(pr.get("updated_at")),
    )


def _parse_iso(s: str | None) -> datetime:
    if not s:
        return datetime.now(UTC)
    if s.endswith("Z"):
        s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def parse_webhook(event_type: str, source_event_id: str, payload: dict[str, Any]) -> list[VCSEvent]:
    """Return zero or more VCSEvents from a verified, parsed webhook payload."""
    now = datetime.now(UTC)
    repo_external_id = (payload.get("repository") or {}).get("full_name", "")
    pr = payload.get("pull_request") or {}
    pr_external_id = f"{repo_external_id}#{pr['number']}" if pr and "number" in pr else None

    if event_type == "pull_request":
        action = payload.get("action")
        if action == "opened":
            if pr.get("draft", False):
                return []
            return [
                PullRequestReadyForReview(
                    plugin_id="github",
                    source_event_id=source_event_id,
                    received_at=now,
                    repo_external_id=repo_external_id,
                    pr_external_id=pr_external_id,
                    pr=_parse_pr(payload),
                )
            ]
        if action == "ready_for_review":
            return [
                PullRequestReadyForReview(
                    plugin_id="github",
                    source_event_id=source_event_id,
                    received_at=now,
                    repo_external_id=repo_external_id,
                    pr_external_id=pr_external_id,
                    pr=_parse_pr(payload),
                )
            ]
        if action == "synchronize":
            return [
                PullRequestSynchronized(
                    plugin_id="github",
                    source_event_id=source_event_id,
                    received_at=now,
                    repo_external_id=repo_external_id,
                    pr_external_id=pr_external_id,
                    new_head_sha=pr.get("head", {}).get("sha", "") or payload.get("after", ""),
                    # GitHub's `synchronize` event carries `before` + `after`
                    # at the top level of the payload. Plumb `before` through
                    # so the reviewer's incremental-review scope is real.
                    prev_head_sha=payload.get("before") or None,
                    # Default false; the webhook handler enriches via the
                    # github `/compare` API before dispatching to intake.
                    force_push=False,
                )
            ]
        if action == "closed":
            return [
                PullRequestClosed(
                    plugin_id="github",
                    source_event_id=source_event_id,
                    received_at=now,
                    repo_external_id=repo_external_id,
                    pr_external_id=pr_external_id,
                    merged=pr.get("merged", False),
                )
            ]
        if action == "reopened":
            return [
                PullRequestReopened(
                    plugin_id="github",
                    source_event_id=source_event_id,
                    received_at=now,
                    repo_external_id=repo_external_id,
                    pr_external_id=pr_external_id,
                )
            ]
        return []

    if event_type == "issue_comment":
        if payload.get("action") != "created":
            return []
        issue = payload.get("issue") or {}
        if "pull_request" not in issue:
            return []  # plain issue comment, not PR
        comment = payload.get("comment") or {}
        user = comment.get("user", {}) or {}
        pr_num = issue.get("number")
        return [
            CommentCreated(
                plugin_id="github",
                source_event_id=source_event_id,
                received_at=now,
                repo_external_id=repo_external_id,
                pr_external_id=f"{repo_external_id}#{pr_num}" if pr_num else None,
                comment_external_id=str(comment.get("id", "")),
                comment_kind="top_level",
                body=comment.get("body", ""),
                author_login=user.get("login", ""),
                author_type="bot" if user.get("type", "User").lower() == "bot" else "user",
                in_reply_to_comment_external_id=None,
            )
        ]

    if event_type == "pull_request_review_comment":
        if payload.get("action") != "created":
            return []
        comment = payload.get("comment") or {}
        user = comment.get("user", {}) or {}
        # GitHub review threads: `pull_request_review_id` identifies the
        # review batch; for replies, the canonical thread root is the
        # earliest `in_reply_to_id` ancestor — for the typical reply
        # case the parent comment id is sufficient since CommentMessage
        # rows index `external_comment_id` and the reviewer's resolver
        # walks back via `in_reply_to_external_id`.
        external_thread_id = (
            str(comment.get("pull_request_review_id")) if comment.get("pull_request_review_id") else None
        )
        return [
            CommentCreated(
                plugin_id="github",
                source_event_id=source_event_id,
                received_at=now,
                repo_external_id=repo_external_id,
                pr_external_id=pr_external_id,
                comment_external_id=str(comment.get("id", "")),
                comment_kind="inline",
                body=comment.get("body", ""),
                author_login=user.get("login", ""),
                author_type="bot" if user.get("type", "User").lower() == "bot" else "user",
                in_reply_to_comment_external_id=(
                    str(comment.get("in_reply_to_id")) if comment.get("in_reply_to_id") else None
                ),
                external_thread_id=external_thread_id,
            )
        ]

    if event_type == "reaction":
        if payload.get("action") != "created":
            return []
        reaction = payload.get("reaction") or {}
        content = reaction.get("content")
        mapped = {"+1": "thumbs_up", "-1": "thumbs_down"}.get(content)
        if mapped is None:
            return []
        target = (payload.get("comment") or {}).get("id")
        return [
            ReactionAdded(
                plugin_id="github",
                source_event_id=source_event_id,
                received_at=now,
                repo_external_id=repo_external_id,
                pr_external_id=pr_external_id,
                target_comment_external_id=str(target) if target else "",
                reaction=mapped,
                actor_login=reaction.get("user", {}).get("login", ""),
            )
        ]

    return []
