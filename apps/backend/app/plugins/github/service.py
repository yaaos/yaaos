"""GitHub VCSPlugin implementation + webhook receiver."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import jwt as pyjwt
import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.plugin_kit import PluginMeta
from app.domain.vcs import (
    Comment,
    Diff,
    FileSummary,
    Review,
    ReviewPostResult,
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


# Maps a yaaos subagent name to the emoji rendered in the small attribution
# suffix on each posted comment. Anything not listed falls back to 🤖.
_AGENT_EMOJI = {
    "yaaos-docs": "📝",
    "yaaos-architecture": "🏗",
    "yaaos-security": "🛡",
    "yaaos-tests": "🧪",
    "yaaos-line-level": "🔍",
}


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
    """Implements domain/vcs.VCSPlugin against GitHub's REST API."""

    meta = PluginMeta(
        id="github",
        type="vcs",
        display_name="GitHub",
        description="GitHub App integration — reads PRs, posts reviews, receives webhooks.",
        docs_url="https://docs.github.com/en/apps",
    )

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
        """`<github_web_base_url>/<owner>/<repo>.git` — the workspace provider
        pairs this with an installation-token Bearer via GIT_ASKPASS.

        The test stack overrides `github_web_base_url` to fake-github, but
        in_memory_workspace's clone tests monkeypatch this method directly
        with a `file://` URL so a real git server isn't needed.
        """
        return f"{get_settings().github_web_base_url}/{repo_external_id}.git"

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

    async def _api_headers(self, org_id: UUID) -> dict[str, str]:
        token = await self._installation_token(org_id)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

    async def _resolve_org_id(self) -> UUID:
        """: single org. Find via a github_app_installations row."""
        async with db_session() as s:
            row = (await s.execute(select(GitHubAppInstallationRow).limit(1))).scalar_one_or_none()
        if row is None:
            return UUID("00000000-0000-0000-0000-000000000001")
        return row.org_id

    # ── VCSPlugin methods ────────────────────────────────────────────────────

    async def fetch_pr(self, external_id: str) -> VCSPullRequest:
        owner, repo, num = _split_external(external_id)
        org_id = await self._resolve_org_id()
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

    async def fetch_diff(self, external_id: str) -> Diff:
        owner, repo, num = _split_external(external_id)
        org_id = await self._resolve_org_id()
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

    async def list_yaaos_comments(self, external_id: str) -> list[Comment]:
        owner, repo, num = _split_external(external_id)
        org_id = await self._resolve_org_id()
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

    async def detect_force_push(self, repo_external_id: str, before_sha: str, after_sha: str) -> bool:
        """Use GitHub's compare API: a force-push diverges history.

        `status == "diverged"` means the new head is not a fast-forward from
        the old one — i.e. someone rewrote the branch. Any other status (ahead,
        behind, identical) is a normal push.
        """
        if not before_sha or not after_sha or before_sha == after_sha:
            return False
        owner, repo = repo_external_id.split("/", 1)
        org_id = await self._resolve_org_id()
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

    async def list_commit_messages(self, repo_external_id: str, prev_sha: str, head_sha: str) -> list[str]:
        """Commit messages between `prev_sha` and `head_sha` via compare API.

        Used by reviewer.handle_push to detect base-branch merges. The
        compare API returns up to 250 commits in `commits`; for incremental
        review windows that's plenty.
        """
        if not prev_sha or not head_sha or prev_sha == head_sha:
            return []
        owner, repo = repo_external_id.split("/", 1)
        org_id = await self._resolve_org_id()
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

    async def is_repo_accessible(self, repo_external_id: str) -> bool:
        owner, repo = repo_external_id.split("/", 1)
        org_id = await self._resolve_org_id()
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
                resp = await client.get(f"/repos/{owner}/{repo}", headers=await self._api_headers(org_id))
            return resp.status_code == 200
        except Exception:
            return False

    async def post_review(self, external_id: str, review: Review) -> ReviewPostResult:
        # We post each finding as an independent comment rather than a single
        # GitHub Review object, so there is no top-level "[yaaos]" wrapper on
        # the PR. Findings with `file` + `line_start` go to the inline
        # pull-request-comments endpoint (requires `commit_id`, so we fetch the
        # PR's head sha once up front). Findings without file/line, plus the
        # secrets-warning case (no findings, only `summary_body`), go to the
        # issue-comments endpoint — that's GitHub's path for top-level PR
        # comments despite the "issues" naming.
        owner, repo, num = _split_external(external_id)
        org_id = await self._resolve_org_id()

        inline_findings = [
            (i, f) for i, f in enumerate(review.findings) if f.file and f.line_start is not None
        ]
        top_level_findings = [
            (i, f) for i, f in enumerate(review.findings) if not (f.file and f.line_start is not None)
        ]

        commit_id: str | None = None
        if inline_findings:
            pr = await self.fetch_pr(external_id)
            commit_id = pr.head_sha

        finding_to_comment: dict[int, str] = {}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            headers = await self._api_headers(org_id)
            for i, f in inline_findings:
                resp = await client.post(
                    f"/repos/{owner}/{repo}/pulls/{num}/comments",
                    json={
                        "commit_id": commit_id,
                        "path": f.file,
                        "line": f.line_end or f.line_start,
                        "body": _format_finding_body(f),
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                finding_to_comment[i] = str(resp.json().get("id", ""))
            for i, f in top_level_findings:
                resp = await client.post(
                    f"/repos/{owner}/{repo}/issues/{num}/comments",
                    json={"body": _format_finding_body(f)},
                    headers=headers,
                )
                resp.raise_for_status()
                finding_to_comment[i] = str(resp.json().get("id", ""))
            if not review.findings and review.summary_body:
                resp = await client.post(
                    f"/repos/{owner}/{repo}/issues/{num}/comments",
                    json={"body": review.summary_body},
                    headers=headers,
                )
                resp.raise_for_status()
        return ReviewPostResult(
            review_external_id="",
            finding_to_comment_external_id=finding_to_comment,
        )

    async def post_comment_reply(self, external_id: str, parent_comment_external_id: str, body: str) -> str:
        owner, repo, num = _split_external(external_id)
        org_id = await self._resolve_org_id()
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

    async def mark_comments_outdated(self, external_id: str, comment_external_ids: list[str]) -> None:
        # No-op for GitHub (GitHub marks outdated automatically on force push).
        return


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
) -> None:
    """Write/refresh a `github_app_installations` row for an active install.
    Called from the webhook handler on `installation.created` / `installation.unsuspend`."""
    async with db_session() as s:
        existing = (
            await s.execute(
                select(GitHubAppInstallationRow).where(
                    GitHubAppInstallationRow.install_external_id == install_external_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            s.add(
                GitHubAppInstallationRow(
                    org_id=org_id,
                    install_external_id=install_external_id,
                    account_login=account_login,
                    status="active",
                )
            )
        else:
            existing.org_id = org_id
            existing.account_login = account_login
            existing.status = "active"
        await s.commit()


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


def _format_finding_body(f: Any) -> str:
    parts = [f"**{f.title}**", "", f.body]
    if getattr(f, "rationale", None):
        parts.extend(["", f"> {f.rationale}"])
    agent = getattr(f, "source_agent", None)
    if agent:
        emoji = _AGENT_EMOJI.get(agent, "🤖")
        parts.extend(["", f"<sub>{emoji} {agent}</sub>"])
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
    from app.domain.orgs import register_onboarding_contributor, register_vcs_clear_hook  # noqa: PLC0415

    register_vcs_plugin(_plugin)
    register_onboarding_contributor("github_app_installed", _onboarding_github_app_installed)
    register_vcs_clear_hook(_on_vcs_cleared)


def get_plugin() -> GitHubPlugin:
    return _plugin


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
