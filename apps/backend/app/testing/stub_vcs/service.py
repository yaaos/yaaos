"""StubVCSPlugin — minimal in-process `VCSPlugin` for service tests.

Implements every method in the `domain.vcs.VCSPlugin` Protocol so the type
check passes at registration. Methods return canned defaults; tests set
specific responses via `set_pr` / `set_diff` / `set_comments` before
exercising the flow. Every `post_review` call is recorded on
`posted_reviews` for assertions.

Register with `register_stub_vcs(plugin_id="github")` in a pytest fixture;
the fixture yields the stub instance so the test can configure state and
read recorded calls. The fixture restores the previous plugin (if any) on
teardown so other tests aren't disrupted.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from app.core.plugin_kit import PluginMeta
from app.domain.vcs import (
    _PLUGINS,
    Comment,
    Diff,
    FileSummary,
    Review,
    ReviewPostResult,
    VCSPullRequest,
)


def _default_pr(external_id: str = "owner/repo#1", plugin_id: str = "github") -> VCSPullRequest:
    return VCSPullRequest(
        plugin_id=plugin_id,
        external_id=external_id,
        repo_external_id="owner/repo",
        number=1,
        title="stub PR",
        body="",
        author_login="alice",
        author_type="user",
        base_branch="main",
        head_branch="feature",
        base_sha="base-sha",
        head_sha="head-sha",
        is_draft=False,
        is_fork=False,
        state="open",
        html_url=f"https://example.test/{external_id}",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _default_diff() -> Diff:
    return Diff(
        raw=(
            "diff --git a/src/example.ts b/src/example.ts\n"
            "index 0000000..1111111 100644\n"
            "--- a/src/example.ts\n"
            "+++ b/src/example.ts\n"
            "@@ -1,1 +1,2 @@\n"
            " export {};\n"
            "+// stub diff content\n"
        ),
        files=[FileSummary(path="src/example.ts", status="modified", additions=1, deletions=0)],
    )


class StubVCSPlugin:
    """Implements `domain.vcs.VCSPlugin` for in-process service tests.

    Construct with a `plugin_id` matching whatever your seeded PRs use
    (default `"github"`). State is mutable from outside — tests call
    `set_pr`/`set_diff`/`set_comments`/`set_commit_messages` before driving
    the flow. Recorded `post_review` calls land on `posted_reviews`.
    """

    def __init__(self, *, plugin_id: str = "github") -> None:
        self.meta = PluginMeta(
            id=plugin_id,
            type="vcs",
            display_name=f"{plugin_id} (stub)",
            description="In-process stub. Service tier only.",
        )
        self._prs: dict[str, VCSPullRequest] = {}
        self._diffs: dict[str, Diff] = {}
        self._comments: dict[str, list[Comment]] = {}
        self._commit_messages: dict[tuple[str, str, str], list[str]] = {}
        self._force_push: dict[tuple[str, str, str], bool] = {}
        # Recording — tests read these to assert flow side-effects.
        self.posted_reviews: list[tuple[str, Review]] = []
        self.post_review_external_ids: dict[str, str] = {}

    # ── Test-driven state setters ────────────────────────────────────────

    def set_pr(self, pr: VCSPullRequest) -> None:
        self._prs[pr.external_id] = pr

    def set_diff(self, external_id: str, diff: Diff) -> None:
        self._diffs[external_id] = diff

    def set_comments(self, external_id: str, comments: list[Comment]) -> None:
        self._comments[external_id] = comments

    def set_commit_messages(
        self, repo_external_id: str, prev_sha: str, head_sha: str, messages: list[str]
    ) -> None:
        self._commit_messages[(repo_external_id, prev_sha, head_sha)] = messages

    def set_force_push(self, repo_external_id: str, before_sha: str, after_sha: str, value: bool) -> None:
        self._force_push[(repo_external_id, before_sha, after_sha)] = value

    # ── VCSPlugin Protocol ───────────────────────────────────────────────

    def install_url(self, org_id: UUID) -> str | None:
        del org_id
        return None

    def validate_settings(self, settings: dict[str, object]) -> dict[str, object]:
        return dict(settings)

    async def fetch_pr(self, external_id: str) -> VCSPullRequest:
        return self._prs.get(external_id) or _default_pr(external_id, self.meta.id)

    async def fetch_diff(self, external_id: str) -> Diff:
        return self._diffs.get(external_id) or _default_diff()

    async def list_yaaos_comments(self, external_id: str) -> list[Comment]:
        return list(self._comments.get(external_id, []))

    async def is_repo_accessible(self, repo_external_id: str) -> bool:
        del repo_external_id
        return True

    async def detect_force_push(self, repo_external_id: str, before_sha: str, after_sha: str) -> bool:
        return self._force_push.get((repo_external_id, before_sha, after_sha), False)

    async def list_commit_messages(self, repo_external_id: str, prev_sha: str, head_sha: str) -> list[str]:
        return list(self._commit_messages.get((repo_external_id, prev_sha, head_sha), []))

    async def post_review(self, external_id: str, review: Review) -> ReviewPostResult:
        self.posted_reviews.append((external_id, review))
        external_review_id = f"stub-review-{len(self.posted_reviews)}"
        # Map each finding index to a synthetic comment id.
        mapping = {i: f"stub-comment-{external_review_id}-{i}" for i in range(len(review.findings))}
        result = ReviewPostResult(
            review_external_id=external_review_id,
            finding_to_comment_external_id=mapping,
        )
        self.post_review_external_ids[external_id] = external_review_id
        return result

    async def post_comment_reply(self, external_id: str, parent_comment_external_id: str, body: str) -> str:
        del external_id, parent_comment_external_id, body
        return "stub-reply-comment-id"

    async def mark_comments_outdated(self, external_id: str, comment_external_ids: list[str]) -> None:
        del external_id, comment_external_ids

    async def get_installation_token(self, org_id: UUID) -> str:
        del org_id
        return "stub-installation-token"


@contextmanager
def register_stub_vcs(*, plugin_id: str = "github") -> Iterator[StubVCSPlugin]:
    """Context manager: swap the registered VCS plugin for a `StubVCSPlugin`,
    yield the stub for state setup + assertions, restore on exit.

    If `plugin_id` is already registered (the real plugin from `app.web`'s
    bootstrap), the prior entry is saved + restored. If not, the slot is
    removed on exit.
    """
    stub = StubVCSPlugin(plugin_id=plugin_id)
    prior = _PLUGINS.get(plugin_id)
    _PLUGINS[plugin_id] = stub  # type: ignore[assignment]
    try:
        yield stub
    finally:
        if prior is None:
            _PLUGINS.pop(plugin_id, None)
        else:
            _PLUGINS[plugin_id] = prior
