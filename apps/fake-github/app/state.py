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
        # `compare_commits`: per "<before>...<after>" pair → list of commit
        # message strings to return in the compare API's `commits` field.
        # Used by reviewer.handle_push to detect base-branch merges (plan
        # §7 rule 3). Default empty list = no commits in the compare window.
        self.compare_commits: dict[str, list[str]] = {}
        self.posted_comments: list[dict[str, Any]] = []
        self._next_comment_id = 5000
        # PRs opened via `create_pr` (POST /repos/{owner}/{repo}/pulls), on top
        # of the pre-seeded ones in `seeded_prs`. Numbered from `_next_pr_number`
        # so they never collide with seeded PR numbers (1, 2, ...).
        self._next_pr_number = 100
        # Reviews submitted via `POST .../pulls/{number}/reviews`. Keyed by
        # `owner/repo#number` -> list of review dicts (oldest first), mirroring
        # GitHub's `GET .../pulls/{number}/reviews` ordering. Read by
        # `has_active_approval`.
        self.reviews: dict[str, list[dict[str, Any]]] = {}
        self._next_review_id = 6000
        # Review threads created alongside each inline PR comment. Keyed by a
        # synthetic GraphQL node id -> {"pr_key", "comment_ids", "resolved"}.
        # Read/written by the `/graphql` shim backing `resolve_finding_thread`.
        self.review_threads: dict[str, dict[str, Any]] = {}
        # Login GitHub reports for reviews submitted "as the app" — mirrors the
        # real `<app-slug>[bot]` shape the GitHub App API uses.
        self.app_bot_login = "yaaos-test[bot]"
        # Installations minted by the `/apps/{slug}/installations/new`
        # picker stub. Maps install id (str) → account.login that
        # `GET /app/installations/{id}` should return.
        self.installations: dict[str, str] = {}
        self._next_installation_id = 7000
        # OAuth user-auth flow: per-code → user profile mapping. Codes are
        # minted by `/login/oauth/authorize` and consumed by
        # `/login/oauth/access_token`. Tests can stage profiles directly via
        # `/__test/stage_oauth_user` to pin login.
        self.oauth_codes: dict[str, dict[str, Any]] = {}
        self._next_oauth_code = 9000
        # The user the next unstaged /login/oauth/authorize returns.
        self.default_oauth_user: dict[str, Any] = {
            "id": 90001,
            "login": "yaaos-owner",
            "name": "yaaos Owner",
            "primary_email": "owner@yaaos.test",
        }

    def reset(self) -> None:
        self.seeded_prs.clear()
        self.seeded_diffs.clear()
        self.seeded_files.clear()
        self.installation_repositories.clear()
        self.compare_status.clear()
        self.compare_commits.clear()
        self.posted_comments.clear()
        self._next_comment_id = 5000
        self._next_pr_number = 100
        self.reviews.clear()
        self._next_review_id = 6000
        self.review_threads.clear()
        self.installations.clear()
        self._next_installation_id = 7000
        self.oauth_codes.clear()
        self._next_oauth_code = 9000
        self.default_oauth_user = {
            "id": 90001,
            "login": "yaaos-owner",
            "name": "yaaos Owner",
            "primary_email": "owner@yaaos.test",
        }

    def next_oauth_code(self) -> int:
        v = self._next_oauth_code
        self._next_oauth_code += 1
        return v

    def next_comment_id(self) -> int:
        v = self._next_comment_id
        self._next_comment_id += 1
        return v

    def next_pr_number(self) -> int:
        v = self._next_pr_number
        self._next_pr_number += 1
        return v

    def next_review_id(self) -> int:
        v = self._next_review_id
        self._next_review_id += 1
        return v

    def next_installation_id(self) -> int:
        v = self._next_installation_id
        self._next_installation_id += 1
        return v


state = FakeGitHubState()
