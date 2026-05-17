"""In-memory state for fake-github. Singleton; reset via /__test/reset."""

from __future__ import annotations

from typing import Any


class FakeGitHubState:
    def __init__(self) -> None:
        self.seeded_prs: dict[str, dict[str, Any]] = {}  # f"{owner}/{repo}#{num}" -> json
        self.seeded_diffs: dict[str, str] = {}
        self.seeded_files: dict[str, list[dict[str, Any]]] = {}
        # `installation_repositories`: repos the App can see, per the
        # `/installation/repositories` endpoint. Drives yaaos's catch-up poller
        # and the Settings GitHub-card live repo list.
        self.installation_repositories: list[dict[str, Any]] = []
        # `compare_status`: per (repo, "before...after") string → GitHub
        # `/compare` status field. Default "ahead" (normal push); specs that
        # want to assert force-push handling can seed "diverged".
        self.compare_status: dict[str, str] = {}
        self.posted_comments: list[dict[str, Any]] = []
        self._next_comment_id = 5000

    def reset(self) -> None:
        self.seeded_prs.clear()
        self.seeded_diffs.clear()
        self.seeded_files.clear()
        self.installation_repositories.clear()
        self.compare_status.clear()
        self.posted_comments.clear()
        self._next_comment_id = 5000

    def next_comment_id(self) -> int:
        v = self._next_comment_id
        self._next_comment_id += 1
        return v


state = FakeGitHubState()
