"""GitHub VCSPlugin implementation + webhook receiver."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import jwt as pyjwt
import structlog
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.vcs import (
    Comment,
    Diff,
    FileSummary,
    VCSAuthError,
    VCSNotFoundError,
    VCSPullRequest,
    VCSValidationError,
    register_vcs_plugin,
)
from app.plugins.github.models import (
    GitHubAppInstallationRow,
    GitHubWebhookEventRow,
)

log = structlog.get_logger("github")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _split_external(external_id: str) -> tuple[str, str, int]:
    """`acme/web#123` -> ("acme", "web", 123)."""
    repo_full, num_s = external_id.split("#", 1)
    owner, repo = repo_full.split("/", 1)
    return owner, repo, int(num_s)


def verify_webhook_signature(body: bytes, header: str | None, secret: bytes) -> bool:
    """Constant-time HMAC verification of `X-Hub-Signature-256`."""
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _platform_credentials() -> tuple[str, str, str]:
    """Returns (app_id, private_key_pem, webhook_secret) for the platform yaaos
    GitHub App. Raises VCSAuthError if the App isn't provisioned (env vars
    blank) — same surface the old per-org lookup raised when `github_settings`
    was empty, so callers don't need to change.

    The private key arrives as a multi-line PEM, but env-var / .env transports
    are line-based — operators normally paste it with `\\n` escapes on a
    single line. We normalize back to real newlines so pyjwt sees a valid PEM.
    """
    s = get_settings()
    app_id = s.yaaos_github_app_id
    pem = s.yaaos_github_app_private_key.get_secret_value().replace("\\n", "\n")
    secret = s.yaaos_github_app_webhook_secret.get_secret_value()
    if not app_id or not pem or not secret:
        raise VCSAuthError("yaaos github app not configured")
    return app_id, pem, secret


class GitHubPlugin:
    """Implements core/vcs.VCSPlugin against GitHub's REST API."""

    plugin_id = "github"

    def __init__(self) -> None:
        # Settings are read lazily (in `base_url`) — avoids construction-time
        # env failures during test collection.
        pass

    @property
    def base_url(self) -> str:
        return get_settings().github_api_base_url

    def install_url(self, org_id: UUID) -> str | None:
        """The github install handshake is driven by an explicit SPA call to
        `POST /api/github/install/start` (which returns the state-signed
        github.com URL). Returning None here means `POST /api/vcs` doesn't
        short-circuit on github — the VCS picker just records the choice and
        the GitHub card surfaces a separate "Install on GitHub" button that
        triggers the JSON handshake."""
        del org_id
        return None

    def validate_settings(self, settings: dict[str, object]) -> dict[str, object]:
        """The github plugin's settings are populated by the install handshake;
        the picker form has nothing the user types. Accept an empty dict;
        reject unknown keys to keep callers honest."""
        unknown = set(settings.keys()) - {"installation_id"}
        if unknown:
            raise VCSValidationError(f"unknown github settings keys: {sorted(unknown)}")
        return dict(settings)

    def clone_url(self, repo_external_id: str) -> str:
        """`<github_git_base_url>/<owner>/<repo>.git` — the workspace provider
        pairs this with an installation-token Bearer via GIT_ASKPASS.

        Built from `github_git_base_url` (the host the agent clones from),
        falling back to `github_web_base_url` when unset. The test stack splits
        them: the agent reaches `fake-github:8080` for the clone while the
        browser uses the host-mapped web URL.
        """
        s = get_settings()
        git_base = s.github_git_base_url or s.github_web_base_url
        return f"{git_base}/{repo_external_id}.git"

    async def _installation_token(self, org_id: UUID) -> str:
        """Trade an App JWT for an installation token. RS256 JWT signed with the
        platform App's private key — GitHub validates against the App's public key.

        For the test stack (`apps/fake-github`), if the configured PEM is a
        sentinel placeholder (no `BEGIN ... PRIVATE KEY` marker), falls back
        to a fake JWT string so the fake-github / integration tests keep
        working without real RSA material.
        """
        app_id, pem, _secret = _platform_credentials()
        async with db_session() as s:
            install = (
                await s.execute(
                    select(GitHubAppInstallationRow)
                    .where(
                        GitHubAppInstallationRow.org_id == org_id,
                        GitHubAppInstallationRow.status == "active",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if install is None:
            raise VCSAuthError("no active GitHub App installation")

        jwt_token = _build_app_jwt(app_id, pem)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            resp = await client.post(
                f"/app/installations/{install.install_external_id}/access_tokens",
                headers={"Authorization": f"Bearer {jwt_token}", "Accept": "application/vnd.github+json"},
            )
        if resp.status_code != 201:
            raise VCSAuthError(f"installation token acquire failed: {resp.status_code}: {resp.text}")
        return resp.json()["token"]

    async def get_installation_token(self, org_id: UUID) -> str:
        """Public Protocol method. Returns a freshly-issued installation token.

        Callers (workspace plugin at clone time; future orchestration at
        each git push/fetch) must use the token immediately and not cache it
        across operations. Internally wraps `_installation_token` so the JWT
        exchange logic stays in one place.
        """
        return await self._installation_token(org_id)

    async def list_installation_repos(self, org_id: UUID) -> list[str]:
        """Live repo full-names the org's GitHub App install can see.

        Resolves an installation token internally and queries
        `/installation/repositories`. GitHub's install picker is the
        authority — no yaaos-side allowlist. Returns an empty list when the
        install is absent or the call fails.
        """
        try:
            token = await self._installation_token(org_id)
        except Exception:
            return []
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
                resp = await client.get(
                    "/installation/repositories",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    params={"per_page": 100},
                )
        except httpx.HTTPError:
            return []
        if resp.status_code != 200:
            return []
        return [r["full_name"] for r in resp.json().get("repositories", [])]

    async def _api_headers(self, org_id: UUID) -> dict[str, str]:
        token = await self._installation_token(org_id)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

    # ── VCSPlugin methods ────────────────────────────────────────────────────

    async def fetch_pr(self, org_id: UUID, external_id: str) -> VCSPullRequest:
        owner, repo, num = _split_external(external_id)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            resp = await client.get(
                f"/repos/{owner}/{repo}/pulls/{num}",
                headers=await self._api_headers(org_id),
            )
        if resp.status_code == 404:
            raise VCSNotFoundError(external_id)
        resp.raise_for_status()
        return self._json_to_pr(resp.json(), owner, repo)

    @staticmethod
    def _json_to_pr(p: dict[str, Any], owner: str, repo: str) -> VCSPullRequest:
        user = p.get("user", {}) or {}
        head = p.get("head", {}) or {}
        base = p.get("base", {}) or {}
        return VCSPullRequest(
            plugin_id="github",
            external_id=f"{owner}/{repo}#{p['number']}",
            repo_external_id=f"{owner}/{repo}",
            number=p["number"],
            title=p.get("title", ""),
            body=p.get("body"),
            author_login=user.get("login", "unknown"),
            author_type="bot" if user.get("type", "User").lower() == "bot" else "user",
            base_branch=base.get("ref", ""),
            head_branch=head.get("ref", ""),
            base_sha=base.get("sha", ""),
            head_sha=head.get("sha", ""),
            is_draft=p.get("draft", False),
            is_fork=(head.get("repo", {}) or {}).get("fork", False),
            state="merged" if p.get("merged") else p.get("state", "open"),
            html_url=p.get("html_url", ""),
            created_at=_parse(p.get("created_at")),
            updated_at=_parse(p.get("updated_at")),
        )

    async def fetch_diff(self, org_id: UUID, external_id: str) -> Diff:
        owner, repo, num = _split_external(external_id)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            headers = await self._api_headers(org_id)
            # Raw diff
            diff_resp = await client.get(
                f"/repos/{owner}/{repo}/pulls/{num}",
                headers={**headers, "Accept": "application/vnd.github.v3.diff"},
            )
            # File summaries (JSON)
            files_resp = await client.get(
                f"/repos/{owner}/{repo}/pulls/{num}/files",
                headers=headers,
            )
        raw = diff_resp.text if diff_resp.status_code == 200 else ""
        files_data: list[dict[str, Any]] = files_resp.json() if files_resp.status_code == 200 else []
        files = [
            FileSummary(
                path=f.get("filename", ""),
                status=_normalize_file_status(f.get("status", "modified")),
                old_path=f.get("previous_filename"),
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
            )
            for f in files_data
        ]
        return Diff(raw=raw, files=files)

    async def list_yaaos_comments(self, org_id: UUID, external_id: str) -> list[Comment]:
        owner, repo, num = _split_external(external_id)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            headers = await self._api_headers(org_id)
            inline = await client.get(f"/repos/{owner}/{repo}/pulls/{num}/comments", headers=headers)
            top = await client.get(f"/repos/{owner}/{repo}/issues/{num}/comments", headers=headers)
        comments: list[Comment] = []
        for c in inline.json() if inline.status_code == 200 else []:
            comments.append(
                Comment(
                    external_id=str(c.get("id", "")),
                    body=c.get("body", ""),
                    file_path=c.get("path"),
                    line=c.get("line"),
                    posted_at=_parse(c.get("created_at")),
                    in_reply_to_external_id=(
                        str(c.get("in_reply_to_id")) if c.get("in_reply_to_id") else None
                    ),
                )
            )
        for c in top.json() if top.status_code == 200 else []:
            comments.append(
                Comment(
                    external_id=str(c.get("id", "")),
                    body=c.get("body", ""),
                    file_path=None,
                    line=None,
                    posted_at=_parse(c.get("created_at")),
                )
            )
        return comments

    async def detect_force_push(
        self, org_id: UUID, repo_external_id: str, before_sha: str, after_sha: str
    ) -> bool:
        """Use GitHub's compare API: a force-push diverges history.

        `status == "diverged"` means the new head is not a fast-forward from
        the old one — i.e. someone rewrote the branch. Any other status (ahead,
        behind, identical) is a normal push.
        """
        if not before_sha or not after_sha or before_sha == after_sha:
            return False
        owner, repo = repo_external_id.split("/", 1)
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
                resp = await client.get(
                    f"/repos/{owner}/{repo}/compare/{before_sha}...{after_sha}",
                    headers=await self._api_headers(org_id),
                )
        except Exception as e:
            log.warning("github.force_push_detect_failed", repo=repo_external_id, error=str(e))
            return False
        if resp.status_code != 200:
            return False
        return resp.json().get("status") == "diverged"

    async def list_commit_messages(
        self, org_id: UUID, repo_external_id: str, prev_sha: str, head_sha: str
    ) -> list[str]:
        """Commit messages between `prev_sha` and `head_sha` via compare API.

        Used by reviewer.handle_push to detect base-branch merges. The
        compare API returns up to 250 commits in `commits`; for incremental
        review windows that's plenty.
        """
        if not prev_sha or not head_sha or prev_sha == head_sha:
            return []
        owner, repo = repo_external_id.split("/", 1)
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
                resp = await client.get(
                    f"/repos/{owner}/{repo}/compare/{prev_sha}...{head_sha}",
                    headers=await self._api_headers(org_id),
                )
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [((c.get("commit") or {}).get("message", "") or "") for c in data.get("commits", [])]

    async def is_repo_accessible(self, org_id: UUID, repo_external_id: str) -> bool:
        owner, repo = repo_external_id.split("/", 1)
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
                resp = await client.get(f"/repos/{owner}/{repo}", headers=await self._api_headers(org_id))
            return resp.status_code == 200
        except Exception:
            return False

    async def get_default_branch(self, org_id: UUID, repo_external_id: str) -> str:
        """Live lookup, not part of `VCSPlugin` — `github:create_pr` (an
        Action, not a Protocol method) needs a base branch when opening a PR
        from a yaaos-authored ticket branch; no stored config carries a
        repo's default branch anywhere else in the tree."""
        owner, repo = repo_external_id.split("/", 1)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
            resp = await client.get(f"/repos/{owner}/{repo}", headers=await self._api_headers(org_id))
        resp.raise_for_status()
        return str(resp.json()["default_branch"])

    async def post_finding(
        self,
        org_id: UUID,
        external_id: str,
        *,
        file: str | None,
        line_start: int | None,
        line_end: int | None,
        severity: str,
        category: str,
        confidence: str,
        finding_display_id: int,
        rationale: str,
        rule_violated: str,
        rule_source: str,
        suggested_fix: str | None,
    ) -> str:
        # Findings with `file` + `line_start` go to the inline pull-request-
        # comments endpoint (requires `commit_id`; fetch head sha once per
        # call). Findings without file/line go to the issue-comments endpoint —
        # that's GitHub's path for top-level PR comments despite the "issues"
        # naming.
        owner, repo, num = _split_external(external_id)
        body = _format_finding_body(
            finding_display_id=finding_display_id,
            category=category,
            severity=severity,
            confidence=confidence,
            rationale=rationale,
            rule_violated=rule_violated,
            rule_source=rule_source,
            suggested_fix=suggested_fix,
        )
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            headers = await self._api_headers(org_id)
            if file and line_start is not None:
                pr = await self.fetch_pr(org_id, external_id)
                resp = await client.post(
                    f"/repos/{owner}/{repo}/pulls/{num}/comments",
                    json={
                        "commit_id": pr.head_sha,
                        "path": file,
                        "line": line_end or line_start,
                        "body": body,
                    },
                    headers=headers,
                )

            else:
                resp = await client.post(
                    f"/repos/{owner}/{repo}/issues/{num}/comments",
                    json={"body": body},
                    headers=headers,
                )
        resp.raise_for_status()
        return str(resp.json().get("id", ""))

    async def post_comment(self, org_id: UUID, external_id: str, *, body: str) -> str:
        # Top-level PR comment — uses the issue-comments endpoint.
        owner, repo, num = _split_external(external_id)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            resp = await client.post(
                f"/repos/{owner}/{repo}/issues/{num}/comments",
                json={"body": body},
                headers=await self._api_headers(org_id),
            )
        resp.raise_for_status()
        return str(resp.json().get("id", ""))

    async def post_comment_reply(
        self, org_id: UUID, external_id: str, parent_comment_external_id: str, body: str
    ) -> str:
        owner, repo, num = _split_external(external_id)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            resp = await client.post(
                f"/repos/{owner}/{repo}/pulls/{num}/comments/{parent_comment_external_id}/replies",
                json={"body": body},
                headers=await self._api_headers(org_id),
            )
        # fall back to issue-comment endpoint if the inline reply endpoint returned 404
        if resp.status_code == 404:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
                resp = await client.post(
                    f"/repos/{owner}/{repo}/issues/{num}/comments",
                    json={"body": body},
                    headers=await self._api_headers(org_id),
                )
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("id", ""))

    async def mark_comments_outdated(
        self, org_id: UUID, external_id: str, comment_external_ids: list[str]
    ) -> None:
        # No-op for GitHub (GitHub marks outdated automatically on force push).
        return

    async def create_pr(
        self,
        org_id: UUID,
        repo_external_id: str,
        *,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> str:
        """Open a PR; idempotent on an existing open PR for `head_branch`.

        GitHub's real signal for "a PR already exists" is a 422 Validation
        Failed on create — not a precondition GET. On 422 we look up the
        existing open PR via the head-branch filter and return it instead.
        """
        owner, repo = repo_external_id.split("/", 1)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            headers = await self._api_headers(org_id)
            resp = await client.post(
                f"/repos/{owner}/{repo}/pulls",
                json={"title": title, "body": body, "head": head_branch, "base": base_branch},
                headers=headers,
            )
            if resp.status_code == 422:
                existing = await client.get(
                    f"/repos/{owner}/{repo}/pulls",
                    params={"head": f"{owner}:{head_branch}", "state": "open"},
                    headers=headers,
                )
                existing.raise_for_status()
                prs = existing.json()
                if not prs:
                    resp.raise_for_status()  # no existing PR either — surface the original 422
                return f"{owner}/{repo}#{prs[0]['number']}"
        resp.raise_for_status()
        return f"{owner}/{repo}#{resp.json()['number']}"

    async def approve_pr(self, org_id: UUID, external_id: str) -> None:
        # Submits an approving review as the app. Never merges.
        owner, repo, num = _split_external(external_id)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            resp = await client.post(
                f"/repos/{owner}/{repo}/pulls/{num}/reviews",
                json={"event": "APPROVE"},
                headers=await self._api_headers(org_id),
            )
        resp.raise_for_status()

    async def has_active_approval(self, org_id: UUID, external_id: str) -> bool:
        """GitHub is the source of truth — no local marker. Reviews come back
        oldest-first; the app's latest review is the currently-effective one."""
        owner, repo, num = _split_external(external_id)
        bot_login = f"{get_settings().yaaos_github_app_slug}[bot]"
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            resp = await client.get(
                f"/repos/{owner}/{repo}/pulls/{num}/reviews",
                headers=await self._api_headers(org_id),
            )
        resp.raise_for_status()
        ours = [r for r in resp.json() if (r.get("user") or {}).get("login") == bot_login]
        if not ours:
            return False
        return ours[-1].get("state") == "APPROVED"

    async def resolve_finding_thread(self, org_id: UUID, external_id: str, comment_external_id: str) -> None:
        """GitHub has no REST endpoint for resolving a review thread — only the
        GraphQL `resolveReviewThread` mutation. Two round trips: locate the
        thread anchoring `comment_external_id`, then resolve it.
        """
        owner, repo, num = _split_external(external_id)
        headers = await self._api_headers(org_id)
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              reviewThreads(first: 100) {
                nodes { id isResolved comments(first: 50) { nodes { databaseId } } }
              }
            }
          }
        }
        """
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            resp = await client.post(
                "/graphql",
                json={"query": query, "variables": {"owner": owner, "repo": repo, "number": num}},
                headers=headers,
            )
            resp.raise_for_status()
            nodes = resp.json()["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
            target_id = int(comment_external_id)
            thread_id = next(
                (n["id"] for n in nodes if any(c["databaseId"] == target_id for c in n["comments"]["nodes"])),
                None,
            )
            if thread_id is None:
                raise VCSNotFoundError(f"no review thread found for comment {comment_external_id}")
            mutation = """
            mutation($threadId: ID!) {
              resolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } }
            }
            """
            mut_resp = await client.post(
                "/graphql",
                json={"query": mutation, "variables": {"threadId": thread_id}},
                headers=headers,
            )
        mut_resp.raise_for_status()


async def record_app_install(
    session,
    *,
    org_id: UUID,
    install_external_id: str,
    account_login: str,
    status: str = "active",
) -> None:
    """Insert a ``github_app_installations`` row.

    Shape (a) — takes ``session`` first positional; never commits. Caller
    composes with sibling writes inside one ``async with db_session()`` block.
    See ``apps/backend/docs/patterns.md`` § Service-fn session-handling convention.

    For idempotent writes (duplicate ``install_external_id`` is a unique-
    constraint violation here), call ``upsert_installation`` instead.
    """
    session.add(
        GitHubAppInstallationRow(
            org_id=org_id,
            install_external_id=install_external_id,
            account_login=account_login,
            status=status,
        )
    )
    await session.flush()


async def upsert_installation(
    *,
    install_external_id: str,
    account_login: str,
    org_id: UUID,
) -> bool:
    """Write/refresh a `github_app_installations` row for an active install.
    Returns True when this call inserted the row (first bind), False on conflict
    (idempotent update of existing row). Atomically idempotent — a single
    INSERT … ON CONFLICT DO UPDATE serialises concurrent callers so exactly one
    returns True. Called from the webhook handler on
    `installation.created` / `installation.unsuspend` and from the install
    callback in `web.py`.
    """
    async with db_session() as s:
        insert_stmt = pg_insert(GitHubAppInstallationRow).values(
            org_id=org_id,
            install_external_id=install_external_id,
            account_login=account_login,
            status="active",
        )
        exc = insert_stmt.excluded
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=["install_external_id"],
            set_={
                "org_id": exc.org_id,
                "account_login": exc.account_login,
                "status": exc.status,
            },
        ).returning(
            GitHubAppInstallationRow.id,
            sa_text("xmax = 0 AS was_insert"),
        )
        # xmax=0 means this was a fresh insert (not an ON CONFLICT DO UPDATE).
        _row = (await s.execute(upsert_stmt)).one()
        was_insert: bool = bool(_row[1])
        await s.commit()
    return was_insert


async def fetch_install_account_login(installation_id: int) -> str:
    """Look up `account.login` for a fresh install via the App JWT. Used by the
    post-install callback to seed `github_app_installations.account_login`
    without waiting for the `installation.created` webhook to arrive.

    Returns the login, or `""` if GitHub returns no usable payload. Raises on
    transport errors so the caller can degrade explicitly."""
    plugin = get_plugin()
    app_id, pem, _ = _platform_credentials()
    jwt_token = _build_app_jwt(app_id, pem)
    async with httpx.AsyncClient(base_url=plugin.base_url, timeout=15) as client:
        resp = await client.get(
            f"/app/installations/{installation_id}",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
            },
        )
    if resp.status_code != 200:
        raise httpx.HTTPStatusError(
            f"installation lookup failed: {resp.status_code}", request=resp.request, response=resp
        )
    body = resp.json()
    return (body.get("account") or {}).get("login") or ""


async def mark_installation_inactive(*, install_external_id: str, status: str) -> None:
    """Flip an install row to non-active. Called on `installation.deleted` /
    `installation.suspend`. `status` is one of `"uninstalled"`, `"suspended"`."""
    async with db_session() as s:
        row = (
            await s.execute(
                select(GitHubAppInstallationRow).where(
                    GitHubAppInstallationRow.install_external_id == install_external_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        row.status = status
        await s.commit()


def _format_finding_body(
    *,
    finding_display_id: int,
    category: str,
    severity: str,
    confidence: str,
    rationale: str,
    rule_violated: str,
    rule_source: str,
    suggested_fix: str | None,
) -> str:
    """Render a finding as a GitHub comment body.

    Uses the canonical named primitive args from `VCSPlugin.post_finding`.
    No value object crosses this boundary.
    """
    # Category prefix for the handle, e.g. "sec-1".
    from app.domain.reviewer import finding_handle  # noqa: PLC0415

    handle = finding_handle(category, finding_display_id)
    parts = [
        f"**[{handle}] {rule_violated}**",
        "",
        rationale,
        "",
        f"- **Severity:** {severity}  **Confidence:** {confidence}  **Source:** {rule_source}",
    ]
    if suggested_fix:
        parts.extend(["", f"**Suggested fix:** {suggested_fix}"])
    return "\n".join(parts)


def _normalize_file_status(status: str) -> str:
    return {"added": "added", "modified": "modified", "removed": "removed", "renamed": "renamed"}.get(
        status, "modified"
    )


def _parse(s: str | None) -> datetime:
    if not s:
        return _utcnow()
    if s.endswith("Z"):
        s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return _utcnow()


def _build_app_jwt(app_id: str, pem: str) -> str:
    """Build the App JWT used to exchange for installation tokens.

    Real GitHub requires an RS256-signed JWT with `iss=app_id`, ~10min `exp`,
    and a small `iat` clock skew. The fake-github test stack accepts any string
    starting with `jwt-fake-` — so when the stored PEM is the test sentinel,
    we emit the fake token instead of trying to RSA-sign a non-key.
    """
    if not pem or "BEGIN" not in pem:
        return f"jwt-fake-{app_id}"
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": app_id}
    return pyjwt.encode(payload, pem, algorithm="RS256")


_plugin = GitHubPlugin()


async def _onboarding_github_app_installed(org_id: UUID) -> bool:
    async with db_session() as s:
        row = (
            await s.execute(
                select(GitHubAppInstallationRow).where(
                    GitHubAppInstallationRow.org_id == org_id,
                    GitHubAppInstallationRow.status == "active",
                )
            )
        ).scalar_one_or_none()
    return row is not None


async def _on_vcs_cleared(org_id: UUID, plugin_id: str, session: Any) -> None:
    """VCS-clear hook: remove the org's github install row when the org
    unlinks its GitHub VCS plugin. Called by `domain/orgs.clear_vcs` via
    the registered hook so `domain/orgs` never imports plugin models.
    """
    from sqlalchemy import delete as _sql_delete  # noqa: PLC0415

    if plugin_id != "github":
        return
    await session.execute(
        _sql_delete(GitHubAppInstallationRow).where(GitHubAppInstallationRow.org_id == org_id)
    )
    await session.flush()


def bootstrap() -> None:
    from app.core.intake import IntakePoint, register_intake_point  # noqa: PLC0415
    from app.domain.actions import register_action  # noqa: PLC0415
    from app.domain.orgs import register_onboarding_contributor, register_vcs_clear_hook  # noqa: PLC0415
    from app.plugins.github.actions import (  # noqa: PLC0415
        GitHubCreatePRAction,
        GitHubReplyToCommentAction,
        GitHubUpdatePRAction,
    )

    register_vcs_plugin(_plugin)
    register_onboarding_contributor("github_app_installed", _onboarding_github_app_installed)
    register_vcs_clear_hook(_on_vcs_cleared)
    register_action(GitHubCreatePRAction())
    register_action(GitHubUpdatePRAction())
    register_action(GitHubReplyToCommentAction())

    # Trigger-binding picker entries — `domain/repos.add_binding` validates
    # `intake_point_id` against this registry; the webhook rewire in
    # `intake_type.py` resolves bindings for "github:pr_opened" and
    # "github:pr_commits". "github:pr_comment" is the comment-response run
    # target `domain/pr_review.maybe_start_batch_run` resolves.
    register_intake_point(
        IntakePoint(id="github:pr_opened", plugin_id="github", label="PR opened", kind="webhook")
    )
    register_intake_point(
        IntakePoint(id="github:pr_commits", plugin_id="github", label="PR commits pushed", kind="webhook")
    )
    register_intake_point(
        IntakePoint(id="github:pr_comment", plugin_id="github", label="PR comment", kind="webhook")
    )


def get_plugin() -> GitHubPlugin:
    return _plugin


@contextmanager
def set_github_plugin_for_tests(plugin: GitHubPlugin | None = None) -> Iterator[GitHubPlugin]:
    """Context manager: swap the singleton plugin for the duration of the block.

    Pass an explicit ``plugin`` instance to test plugin-dependent paths, or
    omit the argument to receive a fresh default ``GitHubPlugin`` instance.
    Restores the prior singleton on exit — even on exception.

    Production never calls this.
    """
    global _plugin
    prior = _plugin
    _plugin = plugin if plugin is not None else GitHubPlugin()
    try:
        yield _plugin
    finally:
        _plugin = prior


async def record_webhook_event(
    source_event_id: str,
    event_type: str,
    payload: dict[str, Any],
    org_id: UUID,
) -> UUID | None:
    """Idempotent insert into github_webhook_events. Returns the new row's id, or
    None if the source_event_id was already recorded.
    """
    async with db_session() as s:
        existing = (
            await s.execute(
                select(GitHubWebhookEventRow.id).where(
                    GitHubWebhookEventRow.source_event_id == source_event_id
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None
        row = GitHubWebhookEventRow(
            org_id=org_id,
            source_event_id=source_event_id,
            event_type=event_type,
            payload=payload,
        )
        s.add(row)
        await s.flush()
        row_id = row.id
        try:
            await s.commit()
        except Exception as e:
            # Most likely a race on the unique constraint — treat as already-seen.
            log.warning("github.webhook_insert_race", source_event_id=source_event_id, error=str(e))
            await s.rollback()
            return None
        return row_id


async def mark_webhook_processed(row_id: UUID) -> None:
    from sqlalchemy import update  # noqa: PLC0415

    async with db_session() as s:
        await s.execute(
            update(GitHubWebhookEventRow)
            .where(GitHubWebhookEventRow.id == row_id)
            .values(processed_at=_utcnow())
        )
        await s.commit()
