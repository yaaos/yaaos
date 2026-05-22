"""`github_pr` IntakeType — verifies GitHub webhook signature, parses the
PR payload, returns an `IntakePrepared` ready for `domain/tickets.create()`.

Lives in `domain/intake` (not `plugins/github`) because the registry of
intake types is domain-owned; the type calls into plugin-owned helpers
(`verify_webhook_signature`, `parse_webhook`) for the GitHub-specific work.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import structlog
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.intake.registry import IntakePrepared, IntakeRejectedError

log = structlog.get_logger("intake.github_pr")


class GithubPrIntakeType:
    """Maps inbound `pull_request` / `issue_comment` GitHub webhooks to the
    `pr_review_v1` workflow. Other event types (push, installation lifecycle,
    etc.) are still handled by `plugins/github.web` — this type is the
    Phase 2 surface that the new generic `/api/intake/{type}` endpoint
    funnels PR-review work through."""

    name = "github_pr"
    workflow_name = "pr_review_v1"

    async def handle(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        session: AsyncSession,
    ) -> IntakePrepared:
        # 1. Verify HMAC against the org's stored webhook secret.
        from app.plugins.github.models import GitHubAppInstallationRow, GitHubSettingsRow  # noqa: PLC0415
        from app.plugins.github.service import verify_webhook_signature  # noqa: PLC0415

        signature = _lookup_header(headers, "X-Hub-Signature-256")
        delivery = _lookup_header(headers, "X-Github-Delivery")
        event = _lookup_header(headers, "X-Github-Event")

        settings_row = (await session.execute(select(GitHubSettingsRow).limit(1))).scalar_one_or_none()
        if settings_row is None:
            raise IntakeRejectedError("bad_request", "github settings not configured")

        fernet = Fernet(get_settings().yaaos_encryption_key.encode())
        secret = fernet.decrypt(settings_row.encrypted_webhook_secret)
        if not verify_webhook_signature(body, signature, secret):
            log.warning("intake.github_pr.bad_signature", delivery=delivery)
            raise IntakeRejectedError("bad_signature", "signature verification failed")

        # 2. Parse JSON. Bad JSON → bad_request.
        try:
            payload: dict[str, Any] = json.loads(body)
        except json.JSONDecodeError as exc:
            raise IntakeRejectedError("bad_request", f"invalid json: {exc}") from exc

        # 3. Resolve org via the installation lookup; fall back to the row
        # owning the settings. Mirrors `plugins/github.web.webhook`.
        org_id = settings_row.org_id
        install_id = (payload.get("installation") or {}).get("id")
        if install_id is not None:
            install = (
                await session.execute(
                    select(GitHubAppInstallationRow).where(
                        GitHubAppInstallationRow.install_external_id == str(install_id)
                    )
                )
            ).scalar_one_or_none()
            if install is not None:
                org_id = install.org_id

        # 4. Only `pull_request.opened|reopened|ready_for_review` is currently
        # routed to `pr_review_v1`. Other actions are out of scope for Phase 2
        # and get a 422 so the caller knows to retry via the legacy endpoint
        # (which still handles push, install lifecycle, etc.).
        action = payload.get("action")
        if event != "pull_request" or action not in {"opened", "reopened", "ready_for_review"}:
            raise IntakeRejectedError(
                "unsupported",
                f"github_pr intake does not handle event={event!r} action={action!r}",
            )

        pr = payload.get("pull_request") or {}
        pr_external_id = str(pr.get("id") or "")
        repo = (payload.get("repository") or {}).get("full_name") or ""
        title = pr.get("title")
        body_text = pr.get("body")

        # Admission signals: CheckShouldReview reads these to decide whether
        # to provision a workspace at all. Keeping them on the ticket payload
        # means the engine doesn't have to re-fetch the PR from GitHub before
        # the first gate.
        head_repo = (pr.get("head") or {}).get("repo") or {}
        base_repo = (pr.get("base") or {}).get("repo") or {}
        is_draft = bool(pr.get("draft", False))
        is_fork = (head_repo.get("full_name") or "") != (base_repo.get("full_name") or "")
        labels = [str((label or {}).get("name") or "") for label in (pr.get("labels") or [])]

        if not pr_external_id:
            raise IntakeRejectedError("bad_request", "missing pull_request.id")

        # 5. Idempotency key: the GitHub delivery id is unique per webhook
        # delivery. If a delivery retries (network blip on GitHub's side) we
        # want the same ticket, not a duplicate. Falls back to a synthesized
        # `pr_external_id:action` so retries without a delivery header still
        # collapse.
        idempotency_key = delivery or f"github_pr:{pr_external_id}:{action}"

        return IntakePrepared(
            org_id=org_id,
            idempotency_key=idempotency_key,
            title=title,
            description=body_text,
            source_external_id=pr_external_id,
            repo_external_id=repo,
            payload={
                "event": event,
                "action": action,
                "pr_external_id": pr_external_id,
                "html_url": pr.get("html_url"),
                "head_sha": (pr.get("head") or {}).get("sha"),
                "base_sha": (pr.get("base") or {}).get("sha"),
                "author_login": (pr.get("user") or {}).get("login"),
                "is_draft": is_draft,
                "is_fork": is_fork,
                "labels": labels,
            },
        )


def _lookup_header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup. FastAPI normalizes headers to lowercase
    in the dict it hands us, but tests sometimes pass mixed-case."""
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return ""
