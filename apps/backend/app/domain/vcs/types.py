"""Abstract VCS types used by every plugin and consumer."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.plugin_kit import PluginMeta


class RepoRef(BaseModel):
    plugin_id: str
    external_id: str


class VCSPullRequest(BaseModel):
    plugin_id: str
    external_id: str
    repo_external_id: str
    number: int
    title: str
    body: str | None
    author_login: str
    author_type: Literal["user", "bot"]
    base_branch: str
    head_branch: str
    base_sha: str
    head_sha: str
    is_draft: bool
    is_fork: bool
    state: Literal["open", "closed", "merged"]
    html_url: str
    created_at: datetime
    updated_at: datetime


class FileSummary(BaseModel):
    path: str
    status: Literal["added", "modified", "removed", "renamed"]
    old_path: str | None = None
    additions: int
    deletions: int


class Diff(BaseModel):
    raw: str
    files: list[FileSummary]


class Comment(BaseModel):
    external_id: str
    body: str
    file_path: str | None = None
    line: int | None = None
    posted_at: datetime
    in_reply_to_external_id: str | None = None


Severity = Literal["must-fix", "nit", "suggestion", "info"]
ReviewState = Literal["APPROVED", "CHANGES_REQUESTED", "COMMENT"]


class FindingSnippetLine(BaseModel):
    line_number: int
    kind: Literal["context", "add", "del"]
    text: str


class Finding(BaseModel):
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    severity: Severity
    title: str
    body: str
    rationale: str | None = None
    snippet: list[FindingSnippetLine] | None = None
    applied_lesson_ids: list[UUID] = []
    # Which yaaos subagent surfaced this finding (e.g. "yaaos-architecture").
    # Set by the parent reviewer when it synthesizes its subagents' outputs.
    # Used for the per-comment prefix on GitHub.
    source_agent: str | None = None


class Review(BaseModel):
    agent_tag: str
    state: ReviewState
    summary_body: str | None = None
    findings: list[Finding]


class ReviewPostResult(BaseModel):
    review_external_id: str
    finding_to_comment_external_id: dict[int, str] = {}


# Events


class VCSEventBase(BaseModel):
    plugin_id: str
    source_event_id: str
    received_at: datetime
    repo_external_id: str
    pr_external_id: str | None = None


class PullRequestReadyForReview(VCSEventBase):
    kind: Literal["pr_ready_for_review"] = "pr_ready_for_review"
    pr: VCSPullRequest


class PullRequestSynchronized(VCSEventBase):
    kind: Literal["pr_synchronized"] = "pr_synchronized"
    new_head_sha: str
    # `before` SHA from the GitHub webhook payload. Populated for `synchronize`
    # events; the reviewer uses it as the `prev_sha` boundary for incremental
    # review scoping. None when the upstream event didn't carry it.
    prev_head_sha: str | None = None
    force_push: bool = False


class PullRequestClosed(VCSEventBase):
    kind: Literal["pr_closed"] = "pr_closed"
    merged: bool


class PullRequestReopened(VCSEventBase):
    kind: Literal["pr_reopened"] = "pr_reopened"


class CommentCreated(VCSEventBase):
    kind: Literal["comment_created"] = "comment_created"
    comment_external_id: str
    comment_kind: Literal["inline", "top_level"]
    body: str
    author_login: str
    author_type: Literal["user", "bot"]
    in_reply_to_comment_external_id: str | None = None
    # GitHub review-thread id (from `pull_request_review_comment.pull_request_review_id`
    # or the threaded `in_reply_to_id` lineage). Used by reviewer.handle_developer_reply
    # to resolve external thread → internal CommentThread without a fallback
    # parent-message lookup.
    external_thread_id: str | None = None


class ReactionAdded(VCSEventBase):
    kind: Literal["reaction_added"] = "reaction_added"
    target_comment_external_id: str
    reaction: Literal["thumbs_up", "thumbs_down"]
    actor_login: str


VCSEvent = Annotated[
    PullRequestReadyForReview
    | PullRequestSynchronized
    | PullRequestClosed
    | PullRequestReopened
    | CommentCreated
    | ReactionAdded,
    Field(discriminator="kind"),
]


# Exceptions


class VCSError(Exception):
    """Base for VCS plugin errors."""


class VCSAuthError(VCSError):
    pass


class VCSNotFoundError(VCSError):
    pass


class VCSPermissionError(VCSError):
    pass


class VCSRateLimitError(VCSError):
    def __init__(self, message: str = "", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class VCSTransientError(VCSError):
    pass


class VCSValidationError(VCSError):
    pass


class PluginNotFoundError(LookupError):
    pass


# Protocol


class VCSPlugin(Protocol):
    meta: PluginMeta

    def install_url(self, org_id: UUID) -> str | None:
        """URL to redirect the user to for plugin install (e.g. GitHub App install).

        Return `None` if this plugin has no out-of-band install step — the picker
        UI saves settings directly in that case.
        """
        ...

    def validate_settings(self, settings: dict[str, object]) -> dict[str, object]:
        """Validate a settings payload (typically JSONB from the picker form).

        Returns the canonicalized dict on success. Raises `VCSValidationError`
        on invalid input. Keep cheap and synchronous — no network IO.
        """
        ...

    def clone_url(self, repo_external_id: str) -> str:
        """HTTPS clone URL for the given repo identifier.

        The workspace provider pairs this with a `get_installation_token`
        Bearer (via `GIT_ASKPASS`) to clone the repo. Synchronous because
        building the URL is pure string work; no network IO.
        """
        ...

    async def fetch_pr(self, external_id: str) -> VCSPullRequest: ...
    async def fetch_diff(self, external_id: str) -> Diff: ...
    async def list_yaaos_comments(self, external_id: str) -> list[Comment]: ...
    async def is_repo_accessible(self, repo_external_id: str) -> bool: ...
    async def detect_force_push(self, repo_external_id: str, before_sha: str, after_sha: str) -> bool: ...
    async def list_commit_messages(
        self, repo_external_id: str, prev_sha: str, head_sha: str
    ) -> list[str]: ...
    async def post_review(self, external_id: str, review: Review) -> ReviewPostResult: ...
    async def post_comment_reply(
        self, external_id: str, parent_comment_external_id: str, body: str
    ) -> str: ...
    async def mark_comments_outdated(self, external_id: str, comment_external_ids: list[str]) -> None: ...

    async def get_installation_token(self, org_id: UUID) -> str:
        """Returns a freshly-issued, short-lived (~1h) installation token.

        Callers MUST use the token immediately and forget it; tokens are never
        cached across operations. Workspace plugins use this at clone time;
        future orchestration uses it just before each git push/fetch.
        """
        ...
