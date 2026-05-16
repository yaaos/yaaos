"""GitHub VCSPlugin implementation + webhook receiver."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt as pyjwt
import structlog
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.primitives import PluginMeta
from app.domain.vcs import (
    Comment,
    Diff,
    FileSummary,
    Review,
    ReviewPostResult,
    VCSAuthError,
    VCSNotFoundError,
    VCSPullRequest,
    register_vcs_plugin,
)
from app.plugins.github.models import (
    GitHubAppInstallationRow,
    GitHubSettingsRow,
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

    async def _get_settings_row(self, org_id: UUID) -> GitHubSettingsRow | None:
        async with db_session() as s:
            return (
                await s.execute(select(GitHubSettingsRow).where(GitHubSettingsRow.org_id == org_id))
            ).scalar_one_or_none()

    async def _decrypted_credentials(self, org_id: UUID) -> tuple[str, str, str]:
        """Returns (app_id, private_key_pem, webhook_secret)."""
        row = await self._get_settings_row(org_id)
        if row is None:
            raise VCSAuthError("github_settings not configured")
        fernet = Fernet(get_settings().yaaof_encryption_key.encode())
        pem = fernet.decrypt(row.encrypted_private_key).decode()
        secret = fernet.decrypt(row.encrypted_webhook_secret).decode()
        return row.app_id, pem, secret

    async def _installation_token(self, org_id: UUID) -> str:
        """Trade an App JWT for an installation token. RS256 JWT signed with the
        App's stored private key — GitHub validates against the App's public key.

        For the test stack (`apps/fake-github`), if the stored PEM is a sentinel
        placeholder, falls back to the legacy fake JWT string so the existing
        fake-github / integration tests keep working without real RSA material.
        """
        app_id, pem, _secret = await self._decrypted_credentials(org_id)
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

        Callers (workspace plugin at clone time; future M02+ orchestration at
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
        """M01: single org. Find via a github_app_installations row."""
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

    async def list_yaaof_comments(self, external_id: str) -> list[Comment]:
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

    async def list_open_prs_since(self, repo_external_id: str, since: datetime) -> list[VCSPullRequest]:
        owner, repo = repo_external_id.split("/", 1)
        org_id = await self._resolve_org_id()
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            resp = await client.get(
                f"/repos/{owner}/{repo}/pulls",
                params={"state": "open"},
                headers=await self._api_headers(org_id),
            )
        if resp.status_code != 200:
            return []
        return [self._json_to_pr(p, owner, repo) for p in resp.json()]

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
        owner, repo, num = _split_external(external_id)
        org_id = await self._resolve_org_id()
        # Build the GitHub-shape payload.
        comments_payload: list[dict[str, Any]] = []
        finding_index_for_comment: list[int] = []
        for i, f in enumerate(review.findings):
            if f.file and f.line_start is not None:
                comments_payload.append(
                    {
                        "path": f.file,
                        "line": f.line_end or f.line_start,
                        "body": _format_finding_body(review.agent_tag, f),
                    }
                )
                finding_index_for_comment.append(i)
        # Summary body collects the agent tag + a one-line title for each finding
        # that didn't get an inline comment (or as the review body for an APPROVED).
        body_lines = [f"[{review.agent_tag}]"]
        if review.summary_body:
            body_lines.append(review.summary_body)
        for i, f in enumerate(review.findings):
            if i not in set(finding_index_for_comment):
                body_lines.append(f"- **{f.severity}**: {f.title}\n  {f.body}")
        review_payload = {
            "body": "\n\n".join(body_lines),
            "event": review.state,
            "comments": comments_payload,
        }
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            resp = await client.post(
                f"/repos/{owner}/{repo}/pulls/{num}/reviews",
                json=review_payload,
                headers=await self._api_headers(org_id),
            )
        resp.raise_for_status()
        data = resp.json()
        # fake-github / real GitHub may both return a list of comment ids; if not,
        # we leave the map empty.
        finding_to_comment: dict[int, str] = {}
        comment_ids = data.get("comment_external_ids") or []
        for idx, cid in zip(finding_index_for_comment, comment_ids, strict=False):
            finding_to_comment[idx] = str(cid)
        return ReviewPostResult(
            review_external_id=str(data.get("id", "")),
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

    async def _maybe_seed_settings_row(
        self, org_id: UUID, app_id: str, pem: str, webhook_secret: str
    ) -> None:
        """Idempotent helper used by tests / e2e seeding."""
        async with db_session() as s:
            existing = (
                await s.execute(select(GitHubSettingsRow).where(GitHubSettingsRow.org_id == org_id))
            ).scalar_one_or_none()
            if existing is not None:
                return
            fernet = Fernet(get_settings().yaaof_encryption_key.encode())
            row = GitHubSettingsRow(
                id=uuid4(),
                org_id=org_id,
                app_id=app_id,
                slug="",
                encrypted_private_key=fernet.encrypt(pem.encode()),
                encrypted_webhook_secret=fernet.encrypt(webhook_secret.encode()),
            )
            s.add(row)
            await s.commit()


async def set_github_credentials(
    org_id: UUID,
    *,
    app_id: str,
    slug: str,
    private_key: str,
    webhook_secret: str,
) -> None:
    """Encrypt + upsert App credentials on `github_settings`. Wipes the cached
    installation-token (if any) so the next API call re-issues against the new
    private key.
    """
    fernet = Fernet(get_settings().yaaof_encryption_key.encode())
    enc_key = fernet.encrypt(private_key.encode())
    enc_secret = fernet.encrypt(webhook_secret.encode())
    async with db_session() as s:
        row = (
            await s.execute(select(GitHubSettingsRow).where(GitHubSettingsRow.org_id == org_id))
        ).scalar_one_or_none()
        if row is None:
            row = GitHubSettingsRow(
                id=uuid4(),
                org_id=org_id,
                app_id=app_id,
                slug=slug,
                encrypted_private_key=enc_key,
                encrypted_webhook_secret=enc_secret,
            )
            s.add(row)
        else:
            row.app_id = app_id
            row.slug = slug
            row.encrypted_private_key = enc_key
            row.encrypted_webhook_secret = enc_secret
        await s.commit()


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
                    id=uuid4(),
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


def _format_finding_body(agent_tag: str, f: Any) -> str:
    parts = [f"[{agent_tag}] **{f.title}**", "", f.body]
    if getattr(f, "rationale", None):
        parts.extend(["", f"> {f.rationale}"])
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
    we emit the legacy token instead of trying to RSA-sign a non-key.
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


def bootstrap() -> None:
    from app.domain.settings import register_onboarding_contributor  # noqa: PLC0415

    register_vcs_plugin(_plugin)
    register_onboarding_contributor("github_app_installed", _onboarding_github_app_installed)


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
        row_id = uuid4()
        row = GitHubWebhookEventRow(
            id=row_id,
            org_id=org_id,
            source_event_id=source_event_id,
            event_type=event_type,
            payload=payload,
        )
        s.add(row)
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
