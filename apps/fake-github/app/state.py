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

    def next_installation_id(self) -> int:
        v = self._next_installation_id
        self._next_installation_id += 1
        return v


state = FakeGitHubState()
