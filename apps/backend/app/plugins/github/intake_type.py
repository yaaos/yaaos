"""`github` IntakeType — single entry point for every GitHub webhook event.

Lives in `domain/intake`'s registry. Verifies the HMAC signature, parses
the payload, then branches on `X-Github-Event` + `payload.action`:

Every branch returns `IntakeSideEffect` — handlers manage their own ticket
and workflow inserts on the endpoint's session so they stay atomic with the
HTTP boundary.

| Event | Action | What happens |
|---|---|---|
| `pull_request` | `opened` (non-draft) / `reopened` / `ready_for_review` | Race-safe ticket+PR insert; `engine.start("pr_review_v1", …)` on the endpoint session. |
| `pull_request` | `synchronize` | Refresh PR metadata, call `reviewer.start_incremental_review`. |
| `pull_request` | `closed` | PR state → merged/closed, ticket completed, workflows cancelled. |
| `pull_request` | `reopened` | PR state → open. |
| `issue_comment` / `pull_request_review_comment` | `created` | Parse yaaos commands or route as a developer reply. |
| `reaction` | `created` | Audit row on the related ticket. |
| `installation` | `created` / `unsuspend` / `new_permissions_accepted` | Upsert install row. |
| `installation` | `deleted` / `suspend` | Mark install inactive. |
| anything else | — | `IntakeSideEffect(detail="ignored")` (200, no work). |

The install lookup is the authoritative org-id source. For events without
a resolvable install, the request is rejected as `bad_request` — except
`installation.created`, which is itself the source of the install row and
falls back to `DEFAULT_ORG_ID` (single-tenant POC default).

Idempotency lives on two layers:
- `github_webhook_events.source_event_id` dedups duplicate deliveries via
  `record_webhook_event`. A second delivery returns
  `IntakeSideEffect(detail="duplicate")` and the endpoint commits a no-op.
- Ticket-creating outcomes additionally key on `idempotency_key` =
  `X-Github-Delivery` so retries collapse to the same ticket.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit_for_ticket, audit_for_webhook_event
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.workflow import get_engine
from app.domain.intake.parsing import parse_rereview
from app.domain.intake.registry import (
    IntakeOutcome,
    IntakeRejectedError,
    IntakeSideEffect,
)

log = structlog.get_logger("intake.github")

# Single-tenant POC: events that arrive without a matching install row, AND
# the `installation.created` event itself, are written under this org.
DEFAULT_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

YAAOS_BOT_LOGIN = "yaaos[bot]"


# Audit payload models (mirrored from the retired domain/intake/service.py
# so the audit-log readers continue to see the same kinds + shapes).


class _WebhookFilteredPayload(BaseModel):
    reason: str
    event_kind: str
    source_event_id: str


class _RereviewRequestedPayload(BaseModel):
    comment_external_id: str


class _ReactionReceivedPayload(BaseModel):
    reaction: str
    target_comment_external_id: str


class _TicketCreatedPayload(BaseModel):
    pr_id: UUID
    repo_external_id: str


class GithubIntakeType:
    """Per-event branching lives in `handle()` so the IntakeType protocol
    stays a one-method contract. Class-level `name` matches the URL path
    segment in `POST /api/intake/github`."""

    name = "github"

    async def handle(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        session: AsyncSession,
    ) -> IntakeOutcome:
        from app.plugins.github.models import GitHubAppInstallationRow  # noqa: PLC0415
        from app.plugins.github.service import verify_webhook_signature  # noqa: PLC0415

        signature = _header(headers, "X-Hub-Signature-256")
        delivery = _header(headers, "X-Github-Delivery")
        event = _header(headers, "X-Github-Event")

        secret = get_settings().yaaos_github_app_webhook_secret.get_secret_value()
        if not secret:
            raise IntakeRejectedError("bad_request", "github app not configured")
        if not verify_webhook_signature(body, signature, secret.encode()):
            log.warning("intake.github.bad_signature", delivery=delivery)
            raise IntakeRejectedError("bad_signature", "signature verification failed")

        try:
            payload: dict[str, Any] = json.loads(body)
        except json.JSONDecodeError as exc:
            raise IntakeRejectedError("bad_request", f"invalid json: {exc}") from exc

        action = payload.get("action")
        install_id = (payload.get("installation") or {}).get("id")

        # Org resolution. The install row is the source of truth. For
        # `installation.created` the row doesn't exist yet — fall back to the
        # single-tenant default so the upsert runs against a stable org.
        org_id: UUID | None = None
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
        if org_id is None:
            if event == "installation" and action in ("created", "new_permissions_accepted", "unsuspend"):
                org_id = DEFAULT_ORG_ID
            else:
                raise IntakeRejectedError("bad_request", "no install row for delivery")

        # Idempotent record of the raw webhook for dedup + replay. Owns its
        # own session — survives intake-session rollback so a retry still
        # short-circuits.
        from app.plugins.github.service import (  # noqa: PLC0415
            mark_webhook_processed,
            record_webhook_event,
        )

        webhook_row_id = await record_webhook_event(
            delivery or f"event-{id(payload)}",
            event,
            payload,
            org_id=org_id,
        )
        if webhook_row_id is None:
            return IntakeSideEffect(detail="duplicate")

        try:
            outcome = await self._dispatch(
                event=event,
                action=action,
                payload=payload,
                delivery=delivery,
                install_id=install_id,
                org_id=org_id,
                session=session,
            )
        finally:
            await mark_webhook_processed(webhook_row_id)

        return outcome

    async def _dispatch(
        self,
        *,
        event: str,
        action: str | None,
        payload: dict[str, Any],
        delivery: str,
        install_id: int | None,
        org_id: UUID,
        session: AsyncSession,
    ) -> IntakeOutcome:
        if event == "pull_request":
            return await self._handle_pull_request(
                action=action, payload=payload, delivery=delivery, org_id=org_id, session=session
            )
        if event in ("issue_comment", "pull_request_review_comment"):
            if action != "created":
                return IntakeSideEffect(detail="ignored")
            return await self._handle_comment(event=event, payload=payload, org_id=org_id, session=session)
        if event == "reaction":
            if action != "created":
                return IntakeSideEffect(detail="ignored")
            return await self._handle_reaction(payload=payload, org_id=org_id, session=session)
        if event == "installation" and install_id is not None:
            return await self._handle_installation(action=action, payload=payload, org_id=org_id)
        return IntakeSideEffect(detail="ignored")

    # ── pull_request branch ────────────────────────────────────────────────

    async def _handle_pull_request(
        self,
        *,
        action: str | None,
        payload: dict[str, Any],
        delivery: str,
        org_id: UUID,
        session: AsyncSession,
    ) -> IntakeOutcome:
        pr_payload = payload.get("pull_request") or {}
        pr_number = pr_payload.get("number")
        repo_full = (payload.get("repository") or {}).get("full_name") or ""
        pr_external_id = f"{repo_full}#{pr_number}" if pr_number is not None else None

        if action in ("opened", "reopened", "ready_for_review"):
            # opened on a draft never enters review — match the legacy
            # `payload_parser` behavior.
            if action == "opened" and pr_payload.get("draft", False):
                await self._audit_filtered(payload, "draft", org_id=org_id, delivery=delivery)
                return IntakeSideEffect(detail="filtered_draft")
            return await self._prepare_pr_review(
                payload=payload, delivery=delivery, org_id=org_id, session=session
            )

        if action == "synchronize":
            if pr_external_id is None:
                raise IntakeRejectedError("bad_request", "synchronize without pr number")
            await self._handle_synchronize(payload=payload, org_id=org_id)
            return IntakeSideEffect(detail="synchronize")

        if action == "closed":
            if pr_external_id is None:
                raise IntakeRejectedError("bad_request", "closed without pr number")
            await self._handle_closed(payload=payload, org_id=org_id)
            return IntakeSideEffect(detail="closed")

        if action == "reopened":
            if pr_external_id is None:
                raise IntakeRejectedError("bad_request", "reopened without pr number")
            await self._handle_reopened(payload=payload, org_id=org_id)
            return IntakeSideEffect(detail="reopened")

        return IntakeSideEffect(detail="ignored")

    async def _prepare_pr_review(
        self,
        *,
        payload: dict[str, Any],
        delivery: str,
        org_id: UUID,
        session: AsyncSession,
    ) -> IntakeOutcome:
        """Race-safe ticket+PR upsert + workflow start for a PR-opened-style
        event. All writes go through the endpoint's session so ticket,
        PR back-reference, audit row, and workflow.start outbox enqueue
        commit atomically. Filters forks and bot authors with an audit row
        (no ticket created in that case)."""
        from app.domain import pull_requests  # noqa: PLC0415
        from app.domain.tickets.models import TicketRow  # noqa: PLC0415
        from app.plugins.github.payload_parser import _parse_pr  # noqa: PLC0415

        pr_payload = payload.get("pull_request") or {}
        repo_full = (payload.get("repository") or {}).get("full_name") or ""
        vcs_pr = _parse_pr(payload)

        if vcs_pr.is_fork:
            await self._audit_filtered(payload, "fork", org_id=org_id, delivery=delivery)
            return IntakeSideEffect(detail="filtered_fork")
        if vcs_pr.author_type == "bot":
            await self._audit_filtered(payload, "bot_author", org_id=org_id, delivery=delivery)
            return IntakeSideEffect(detail="filtered_bot")

        # Race-safe ticket insert keyed on `(org_id, source, source_external_id)`.
        # A concurrent delivery with the same PR external id collides on the
        # unique constraint; the loser SELECTs the winner's row and exits.
        head_repo = (pr_payload.get("head") or {}).get("repo") or {}
        base_repo = (pr_payload.get("base") or {}).get("repo") or {}
        ticket_payload = {
            "event": "pull_request",
            "action": payload.get("action"),
            "pr_external_id": vcs_pr.external_id,
            "html_url": vcs_pr.html_url,
            "head_sha": vcs_pr.head_sha,
            "base_sha": vcs_pr.base_sha,
            "author_login": vcs_pr.author_login,
            "is_draft": vcs_pr.is_draft,
            "is_fork": vcs_pr.is_fork,
            "labels": [str((label or {}).get("name") or "") for label in (pr_payload.get("labels") or [])],
            "head_repo_full": (head_repo.get("full_name") or ""),
            "base_repo_full": (base_repo.get("full_name") or ""),
        }
        idempotency_key = delivery or f"github_pr:{vcs_pr.external_id}"

        new_ticket_id = uuid4()
        stmt = (
            pg_insert(TicketRow)
            .values(
                id=new_ticket_id,
                org_id=org_id,
                source="github_pr",
                source_external_id=vcs_pr.external_id,
                title=vcs_pr.title,
                description=vcs_pr.body,
                status="running",
                plugin_id=vcs_pr.plugin_id,
                repo_external_id=repo_full,
                pr_id=None,
                type="github_pr",
                idempotency_key=idempotency_key,
                payload=ticket_payload,
                current_workflow_execution_id=None,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "source", "source_external_id"])
            .returning(TicketRow.id)
        )
        inserted_id = (await session.execute(stmt)).scalar_one_or_none()
        if inserted_id is None:
            # Loser of the race. The winner owns the workflow start; we exit clean.
            return IntakeSideEffect(detail="duplicate_ticket")
        ticket_id = inserted_id

        # PR upsert runs on the endpoint's session — same transaction as the
        # ticket insert above, so the FK on `pull_requests.ticket_id`
        # resolves before commit.
        upserted_pr = await pull_requests.upsert(vcs_pr, ticket_id=ticket_id, org_id=org_id, session=session)

        from sqlalchemy import update as sql_update  # noqa: PLC0415

        await session.execute(
            sql_update(TicketRow)
            .where(TicketRow.id == ticket_id, TicketRow.pr_id.is_(None))
            .values(pr_id=upserted_pr.id)
        )

        await audit_for_ticket(
            ticket_id,
            "ticket.created",
            _TicketCreatedPayload(pr_id=upserted_pr.id, repo_external_id=repo_full),
            actor=Actor.system(),
            org_id=org_id,
            session=session,
        )

        # Broadcast the ticket-creation status change so the SSE subscriber
        # invalidates the tickets list query. Mirrors `tickets.create_for_pr`
        # — both insert with status="running" and previous_status=None.
        from app.core.events import publish_after_commit  # noqa: PLC0415
        from app.domain.tickets.service import TicketStatusChanged  # noqa: PLC0415

        publish_after_commit(
            session,
            TicketStatusChanged(
                ticket_id=ticket_id,
                repo_external_id=repo_full,
                pr_id=upserted_pr.id,
                previous_status=None,
                new_status="running",
            ),
        )

        # Start the workflow on the endpoint's session — outbox row enqueued
        # atomically with the ticket insert.
        from app.core.observability import current_traceparent  # noqa: PLC0415
        from app.domain.orgs.models import OrgRow  # noqa: PLC0415

        org_row = (await session.execute(select(OrgRow).where(OrgRow.id == org_id))).scalar_one_or_none()
        workspace_provider = (org_row.workspace_provider if org_row is not None else None) or "in_memory"

        workflow_execution_id = await get_engine().start(
            workflow_name="pr_review_v1",
            ticket_id=str(ticket_id),
            traceparent=current_traceparent(),
            workspace_provider=workspace_provider,
            ticket_payload=dict(ticket_payload),
            session=session,
        )
        await session.execute(
            sql_update(TicketRow)
            .where(TicketRow.id == ticket_id)
            .values(current_workflow_execution_id=UUID(workflow_execution_id))
        )

        log.info(
            "intake.github.pr_review_started",
            ticket_id=str(ticket_id),
            workflow_execution_id=workflow_execution_id,
            pr_external_id=vcs_pr.external_id,
        )
        return IntakeSideEffect(detail="pr_review_started")

    async def _handle_synchronize(self, *, payload: dict[str, Any], org_id: UUID) -> None:
        from app.domain import pull_requests, reviewer  # noqa: PLC0415
        from app.plugins.github.payload_parser import _parse_pr  # noqa: PLC0415

        pr_payload = payload.get("pull_request") or {}
        repo_full = (payload.get("repository") or {}).get("full_name") or ""
        pr_number = pr_payload.get("number")
        pr_external_id = f"{repo_full}#{pr_number}"

        before_sha = payload.get("before") or ""
        after_sha = pr_payload.get("head", {}).get("sha") or payload.get("after") or ""

        existing_pr = await pull_requests.get_by_external("github", pr_external_id, org_id=org_id)
        if existing_pr is None:
            log.warning("intake.github.synchronize_unknown_pr", pr_external_id=pr_external_id)
            return

        fresh = _parse_pr(payload)
        # Orchestrate the PR refresh on a self-owned session — no other
        # writes need to share this transaction, so the wrap stays here
        # rather than being plumbed in by the caller.
        async with db_session() as s:
            await pull_requests.upsert(fresh, ticket_id=existing_pr.ticket_id, org_id=org_id, session=s)
            await s.commit()

        await reviewer.start_incremental_review(
            existing_pr.id,
            new_head_sha=after_sha or fresh.head_sha,
            prev_head_sha=before_sha or None,
            org_id=org_id,
        )

    async def _handle_closed(self, *, payload: dict[str, Any], org_id: UUID) -> None:
        from app.domain import pull_requests, reviewer, tickets  # noqa: PLC0415

        pr_payload = payload.get("pull_request") or {}
        repo_full = (payload.get("repository") or {}).get("full_name") or ""
        pr_number = pr_payload.get("number")
        pr_external_id = f"{repo_full}#{pr_number}"
        merged = pr_payload.get("merged", False)
        new_state = "merged" if merged else "closed"

        pr = await pull_requests.get_by_external("github", pr_external_id, org_id=org_id)
        if pr is None:
            return
        await pull_requests.update_state(pr.id, new_state, org_id=org_id)  # type: ignore[arg-type]
        ticket = await tickets.get_by_pr(pr.id, org_id=org_id)
        if ticket and ticket.status == "running":
            await tickets.complete(ticket.id, org_id=org_id)
            await reviewer.cancel_workflows_for_ticket(ticket.id)

    async def _handle_reopened(self, *, payload: dict[str, Any], org_id: UUID) -> None:
        from app.domain import pull_requests  # noqa: PLC0415

        pr_payload = payload.get("pull_request") or {}
        repo_full = (payload.get("repository") or {}).get("full_name") or ""
        pr_number = pr_payload.get("number")
        pr_external_id = f"{repo_full}#{pr_number}"
        pr = await pull_requests.get_by_external("github", pr_external_id, org_id=org_id)
        if pr is None:
            return
        await pull_requests.update_state(pr.id, "open", org_id=org_id)  # type: ignore[arg-type]

    # ── comment / reaction / installation branches ─────────────────────────

    async def _handle_comment(
        self,
        *,
        event: str,
        payload: dict[str, Any],
        org_id: UUID,
        session: AsyncSession,
    ) -> IntakeOutcome:
        from app.domain import pull_requests, reviewer, tickets  # noqa: PLC0415
        from app.domain.intake.parsing import parse_yaaos_command  # noqa: PLC0415

        comment = payload.get("comment") or {}
        user = comment.get("user") or {}
        author_login = user.get("login", "")
        author_type = "bot" if (user.get("type", "User").lower() == "bot") else "user"
        if author_login == YAAOS_BOT_LOGIN or author_type == "bot":
            return IntakeSideEffect(detail="ignored_bot_comment")

        repo_full = (payload.get("repository") or {}).get("full_name") or ""
        if event == "issue_comment":
            issue = payload.get("issue") or {}
            if "pull_request" not in issue:
                return IntakeSideEffect(detail="ignored_issue_comment")
            pr_number = issue.get("number")
        else:
            pr_number = (payload.get("pull_request") or {}).get("number")
        if pr_number is None:
            return IntakeSideEffect(detail="ignored_no_pr")
        pr_external_id = f"{repo_full}#{pr_number}"

        pr = await pull_requests.get_by_external("github", pr_external_id, org_id=org_id)
        if pr is None:
            return IntakeSideEffect(detail="ignored_unknown_pr")
        ticket = await tickets.get_by_pr(pr.id, org_id=org_id)
        if ticket is None:
            return IntakeSideEffect(detail="ignored_no_ticket")

        body = comment.get("body", "")
        cmd = parse_yaaos_command(body)
        if cmd is None and parse_rereview(body)[0]:
            cmd = "full review"
        if cmd is not None:
            await audit_for_ticket(
                ticket.id,
                "ticket.rereview_requested",
                _RereviewRequestedPayload(comment_external_id=str(comment.get("id", ""))),
                actor=Actor.github_user(author_login),
                org_id=org_id,
                session=session,
            )
            if cmd == "cancel":
                await reviewer.cancel_workflows_for_ticket(ticket.id)
            elif cmd == "full review":
                await reviewer.start_pr_review(ticket.id, org_id=org_id, trigger_reason="manual_full")
            elif cmd == "review":
                await reviewer.start_incremental_review(
                    pr.id, new_head_sha=pr.head_sha, prev_head_sha=None, org_id=org_id
                )
            return IntakeSideEffect(detail=f"command_{cmd.replace(' ', '_')}")

        # Developer-reply routing on a yaaos comment thread.
        external_thread_id = None
        if event == "pull_request_review_comment":
            review_id = comment.get("pull_request_review_id")
            external_thread_id = str(review_id) if review_id is not None else None

        await reviewer.handle_developer_reply(
            external_thread_id=external_thread_id,
            external_comment_id=str(comment.get("id", "")),
            in_reply_to_external_id=(
                str(comment.get("in_reply_to_id")) if comment.get("in_reply_to_id") else None
            ),
            body=body,
            author_external_id=author_login,
            org_id=org_id,
        )
        return IntakeSideEffect(detail="developer_reply")

    async def _handle_reaction(
        self,
        *,
        payload: dict[str, Any],
        org_id: UUID,
        session: AsyncSession,
    ) -> IntakeOutcome:
        from app.domain import tickets  # noqa: PLC0415
        from app.domain.reviewer.models import (  # noqa: PLC0415
            CommentMessageRow,
            CommentThreadRow,
            FindingRow,
        )

        reaction = payload.get("reaction") or {}
        content = reaction.get("content")
        mapped = {"+1": "thumbs_up", "-1": "thumbs_down"}.get(content)
        if mapped is None:
            return IntakeSideEffect(detail="ignored_reaction_kind")
        target_id = (payload.get("comment") or {}).get("id")
        if target_id is None:
            return IntakeSideEffect(detail="ignored_no_target")

        row = (
            await session.execute(
                select(FindingRow.pr_id)
                .join(CommentThreadRow, CommentThreadRow.finding_id == FindingRow.id)
                .join(CommentMessageRow, CommentMessageRow.thread_id == CommentThreadRow.id)
                .where(CommentMessageRow.external_comment_id == str(target_id))
            )
        ).first()
        if row is None:
            return IntakeSideEffect(detail="ignored_reaction_no_finding")
        pr_id = row[0]
        ticket = await tickets.get_by_pr(pr_id, org_id=org_id)
        if ticket is None:
            return IntakeSideEffect(detail="ignored_reaction_no_ticket")
        actor_login = (reaction.get("user") or {}).get("login", "")
        await audit_for_ticket(
            ticket.id,
            "ticket.reaction_received",
            _ReactionReceivedPayload(
                reaction=mapped,
                target_comment_external_id=str(target_id),
            ),
            actor=Actor.github_user(actor_login),
            org_id=org_id,
            session=session,
        )
        return IntakeSideEffect(detail="reaction_recorded")

    async def _handle_installation(
        self,
        *,
        action: str | None,
        payload: dict[str, Any],
        org_id: UUID,
    ) -> IntakeOutcome:
        from app.plugins.github.service import (  # noqa: PLC0415
            mark_installation_inactive,
            upsert_installation,
        )

        install = payload.get("installation") or {}
        install_id = install.get("id")
        account = (install.get("account") or {}) if isinstance(install.get("account"), dict) else {}
        account_login = account.get("login", "") if isinstance(account, dict) else ""

        if install_id is None:
            return IntakeSideEffect(detail="ignored_no_install_id")
        if action in ("created", "new_permissions_accepted", "unsuspend"):
            await upsert_installation(
                install_external_id=str(install_id),
                account_login=account_login,
                org_id=org_id,
            )
            return IntakeSideEffect(detail=f"install_{action}")
        if action == "deleted":
            await mark_installation_inactive(install_external_id=str(install_id), status="uninstalled")
            return IntakeSideEffect(detail="install_uninstalled")
        if action == "suspend":
            await mark_installation_inactive(install_external_id=str(install_id), status="suspended")
            return IntakeSideEffect(detail="install_suspended")
        return IntakeSideEffect(detail="ignored")

    async def _audit_filtered(
        self,
        payload: dict[str, Any],
        reason: str,
        *,
        org_id: UUID,
        delivery: str,
    ) -> None:
        """Writes `webhook_event.filtered` in its own session so the audit
        row survives a downstream rollback in the intake transaction."""
        event_kind = payload.get("action") or "unknown"
        source_event_id = delivery or "unknown"
        async with db_session() as s:
            await audit_for_webhook_event(
                uuid4(),
                "webhook_event.filtered",
                _WebhookFilteredPayload(
                    reason=reason,
                    event_kind=event_kind,
                    source_event_id=source_event_id,
                ),
                actor=Actor.system(),
                org_id=org_id,
                session=s,
            )
            await s.commit()


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup. FastAPI normalizes headers to lowercase
    in the dict it hands us, but tests sometimes pass mixed-case."""
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return ""
