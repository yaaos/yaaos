"""Developer reply handling (plan §6.4) + verify-fix / answer-question subflows.

Webhook payload → resolve external comment to a CommentThread →
deterministic checks (yaaos command? off-topic? mid-band `confirm`?) →
classifier (`classify_reply` via core/llm) → `apply_classified_reply` on
the aggregate → post yaaos reply via VCS.

Two coding-agent subflows hang off the dispatch:

- `verify_fix_triggered` → `_run_verify_fix`: provisions a workspace at
  HEAD, asks `coding_agent.verify_fix`, routes the result through
  `apply_verify_fix_result`, posts the reply.
- `answer_question_triggered` → `_run_answer_question`: provisions a
  workspace at HEAD with read-only repo + git tool access, asks
  `coding_agent.answer_question`, posts the agent's answer as a reply.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import desc, select

from app.core.audit_log import Actor
from app.core.database import session as db_session
from app.core.observability import spawn
from app.core.workspace import (
    NetworkPolicy,
    RepoRefForSpec,
    ResourceCaps,
    WorkspaceSpec,
    with_workspace,
)
from app.domain import coding_agent, pull_requests
from app.domain.coding_agent import (
    AnswerQuestionContext,
    InvocationStatus,
    PriorThreadMessage,
    VerifyFixContext,
)
from app.domain.coding_agent import (
    FindingAnchor as AgentFindingAnchor,
)
from app.domain.reviewer.llm import (
    ClassifyReplyInput,
    classify_reply,
)
from app.domain.reviewer.llm.classifier import PriorMessage
from app.domain.reviewer.lock import acquire_pr_lock
from app.domain.reviewer.models import CommentMessageRow, CommentThreadRow, FindingRow
from app.domain.reviewer.queue import _REVIEWER_TAG
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.service import (
    apply_classified_reply,
    apply_verify_fix_result,
    dispatch_audits,
    dispatch_events,
    is_off_topic_message,
    is_yaaos_command,
)
from app.domain.vcs import get_plugin as get_vcs_plugin

log = structlog.get_logger("reviewer.replies")


async def handle_developer_reply(
    *,
    external_thread_id: str | None,
    external_comment_id: str,
    in_reply_to_external_id: str | None,
    body: str,
    author_external_id: str,
    org_id: UUID,
) -> str | None:
    """Resolve the comment to a thread, classify, mutate, post.

    Returns a status string for the caller's audit / log line, or None when
    no matching thread exists (the comment isn't on a yaaos finding).
    """
    # 1. Resolve external_thread_id (or in_reply_to_external_id, or
    #    external_comment_id) to a CommentThreadRow.
    thread_row, finding_row = await _resolve_thread(
        external_thread_id, in_reply_to_external_id, external_comment_id
    )
    if thread_row is None or finding_row is None:
        return None
    pr_id = finding_row.pr_id

    # 2. Cheap deterministic checks — plan §6.4 step 2.
    cmd = is_yaaos_command(body)
    if cmd is not None:
        # yaaos command routing is handled separately (intake calls the
        # command dispatcher); just store the message and return.
        await _store_human_message(
            thread_row.id, external_comment_id, in_reply_to_external_id, body, author_external_id
        )
        return f"command:{cmd}"

    # Mid-band confirmation path (plan §6.4 step 4) — if the immediately prior
    # yaaos message asked for a confirm, "confirm" finalizes the ack.
    from app.domain.intake.parsing import is_mid_band_confirm  # noqa: PLC0415

    if is_mid_band_confirm(body) and await _last_message_was_confirm_request(thread_row.id):
        await _finalize_mid_band_ack(
            pr_id=pr_id,
            org_id=org_id,
            finding_id=finding_row.id,
            thread_id=thread_row.id,
            external_comment_id=external_comment_id,
            in_reply_to_external_id=in_reply_to_external_id,
            body=body,
            author_external_id=author_external_id,
        )
        return "ack:confirmed"

    if is_off_topic_message(body):
        await _store_human_message(
            thread_row.id, external_comment_id, in_reply_to_external_id, body, author_external_id
        )
        return "stored:off_topic"

    # 3. Classifier (text-only LLM via core/llm).
    pr = await pull_requests.get(pr_id, org_id=org_id)
    classification = await classify_reply(
        ClassifyReplyInput(
            finding_title=finding_row.title,
            finding_body=finding_row.body,
            rule_id=finding_row.rule_id,
            anchor_file=finding_row.current_anchor.get("file_path", ""),
            anchor_lines=str(finding_row.current_anchor.get("line_start", "")),
            code_snippet="(omitted — POC)",
            reply=body,
            prior_messages=await _prior_messages_for_classifier(thread_row.id),
        )
    )

    # 4. Aggregate mutation + persistence + reply posting.
    async with db_session() as s:
        await acquire_pr_lock(s, pr_id)
        agg_repo = SqlAlchemyAggregateRepository(s)
        aggregate = await agg_repo.load(pr_id=pr_id, org_id=org_id)
        reply_message = aggregate.append_message(
            thread_id=thread_row.id,
            author_kind="human",
            author_external_id=author_external_id,
            external_comment_id=external_comment_id,
            in_reply_to_external_id=in_reply_to_external_id,
            body=body,
            classified_intent=classification.intent,
        )
        action = apply_classified_reply(
            aggregate,
            finding_id=finding_row.id,
            classification=classification,
            reply_message=reply_message,
        )
        # Post the yaaos reply for acknowledge / mid-band confirmation cases.
        if action.kind in {"acknowledge_posted", "confirm_requested"} and action.reply_body:
            vcs_plugin = get_vcs_plugin(pr.plugin_id)
            try:
                yaaos_comment_id = await vcs_plugin.post_comment_reply(
                    pr.external_id, external_comment_id, action.reply_body
                )
            except Exception:
                log.exception("replies.post_reply_failed", thread_id=str(thread_row.id))
                yaaos_comment_id = f"local-reply-{reply_message.id}"
            aggregate.append_message(
                thread_id=thread_row.id,
                author_kind="yaaos",
                author_external_id=_REVIEWER_TAG,
                external_comment_id=yaaos_comment_id,
                body=action.reply_body,
                in_reply_to_external_id=external_comment_id,
            )
        await agg_repo.save(aggregate)
        await dispatch_audits(aggregate, session=s, actor=Actor.system(), org_id=org_id)
        await s.commit()
        await dispatch_events(aggregate)

    if action.kind == "verify_fix_triggered":
        spawn(
            f"verify_fix:{finding_row.id}",
            _run_verify_fix(
                pr_id=pr_id,
                org_id=org_id,
                finding_id=finding_row.id,
                thread_id=thread_row.id,
                reply_parent_external_id=external_comment_id,
            ),
        )
        return "verify_fix:triggered"

    if action.kind == "answer_question_triggered":
        spawn(
            f"answer_question:{reply_message.id}",
            _run_answer_question(
                pr_id=pr_id,
                org_id=org_id,
                finding_id=finding_row.id,
                thread_id=thread_row.id,
                reply_parent_external_id=external_comment_id,
                question=body,
            ),
        )
        return "answer_question:triggered"

    return f"applied:{action.kind}"


async def _resolve_thread(
    external_thread_id: str | None,
    in_reply_to_external_id: str | None,
    external_comment_id: str,
) -> tuple[CommentThreadRow | None, FindingRow | None]:
    """Find the CommentThread + Finding for an inbound external comment.

    Tries `external_thread_id` first (set on GitHub review threads), then
    falls back to looking up the parent (`in_reply_to_external_id`) in
    `comment_messages` to find its thread.
    """
    async with db_session() as s:
        if external_thread_id:
            row = (
                await s.execute(
                    select(CommentThreadRow, FindingRow)
                    .join(FindingRow, FindingRow.id == CommentThreadRow.finding_id)
                    .where(CommentThreadRow.external_thread_id == external_thread_id)
                )
            ).first()
            if row is not None:
                return row[0], row[1]
        if in_reply_to_external_id:
            row = (
                await s.execute(
                    select(CommentThreadRow, FindingRow)
                    .join(FindingRow, FindingRow.id == CommentThreadRow.finding_id)
                    .join(CommentMessageRow, CommentMessageRow.thread_id == CommentThreadRow.id)
                    .where(CommentMessageRow.external_comment_id == in_reply_to_external_id)
                )
            ).first()
            if row is not None:
                return row[0], row[1]
        # As a final fallback, see if the new comment's id was already stored
        # as a yaaos message — unlikely for replies, but cheap to check.
        row = (
            await s.execute(
                select(CommentThreadRow, FindingRow)
                .join(FindingRow, FindingRow.id == CommentThreadRow.finding_id)
                .join(CommentMessageRow, CommentMessageRow.thread_id == CommentThreadRow.id)
                .where(CommentMessageRow.external_comment_id == external_comment_id)
            )
        ).first()
        if row is not None:
            return row[0], row[1]
    return None, None


async def _prior_messages_for_classifier(thread_id: UUID) -> list[PriorMessage]:
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(CommentMessageRow)
                    .where(CommentMessageRow.thread_id == thread_id)
                    .order_by(CommentMessageRow.created_at)
                )
            )
            .scalars()
            .all()
        )
    return [PriorMessage(author_kind=r.author_kind, body=r.body) for r in rows]


async def _last_message_was_confirm_request(thread_id: UUID) -> bool:
    async with db_session() as s:
        row = (
            await s.execute(
                select(CommentMessageRow)
                .where(CommentMessageRow.thread_id == thread_id, CommentMessageRow.author_kind == "yaaos")
                .order_by(desc(CommentMessageRow.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is None:
        return False
    return "reply `confirm`" in (row.body or "").lower()


async def _store_human_message(
    thread_id: UUID,
    external_comment_id: str,
    in_reply_to_external_id: str | None,
    body: str,
    author_external_id: str,
) -> None:
    """Store a human message with no classification (yaaos commands + off-topic)."""
    async with db_session() as s:
        s.add(
            CommentMessageRow(
                thread_id=thread_id,
                author_kind="human",
                author_external_id=author_external_id,
                external_comment_id=external_comment_id,
                in_reply_to_external_id=in_reply_to_external_id,
                body=body,
            )
        )
        await s.commit()


async def _finalize_mid_band_ack(
    *,
    pr_id: UUID,
    org_id: UUID,
    finding_id: UUID,
    thread_id: UUID,
    external_comment_id: str,
    in_reply_to_external_id: str | None,
    body: str,
    author_external_id: str,
) -> None:
    """Mid-band acknowledgment confirmation (plan §6.4 step 4).

    The developer's current message is just `confirm` (or similar). The
    *real* rationale was the message immediately before yaaos's
    confirm-request — the developer's wontfix/intentional explanation that
    triggered the mid-band band in the first place. Walk back through the
    thread to find it, and use THAT as the acknowledgment rationale.
    """
    pr = await pull_requests.get(pr_id, org_id=org_id)
    original_rationale = await _original_mid_band_rationale(thread_id, author_external_id)
    async with db_session() as s:
        await acquire_pr_lock(s, pr_id)
        agg_repo = SqlAlchemyAggregateRepository(s)
        aggregate = await agg_repo.load(pr_id=pr_id, org_id=org_id)
        msg = aggregate.append_message(
            thread_id=thread_id,
            author_kind="human",
            author_external_id=author_external_id,
            external_comment_id=external_comment_id,
            in_reply_to_external_id=in_reply_to_external_id,
            body=body,
        )
        aggregate.acknowledge(
            finding_id=finding_id,
            kind="intentional",
            rationale=original_rationale or body,
            made_by_external_id=author_external_id,
            made_by_message_id=msg.id,
        )
        try:
            vcs_plugin = get_vcs_plugin(pr.plugin_id)
            yaaos_comment_id = await vcs_plugin.post_comment_reply(
                pr.external_id, external_comment_id, "Acknowledged — I'll skip this in future reviews."
            )
        except Exception:
            log.exception("replies.mid_band_post_failed", thread_id=str(thread_id))
            yaaos_comment_id = f"local-mid-band-{msg.id}"
        aggregate.append_message(
            thread_id=thread_id,
            author_kind="yaaos",
            author_external_id=_REVIEWER_TAG,
            external_comment_id=yaaos_comment_id,
            body="Acknowledged — I'll skip this in future reviews.",
            in_reply_to_external_id=external_comment_id,
        )
        await agg_repo.save(aggregate)
        await dispatch_audits(aggregate, session=s, actor=Actor.system(), org_id=org_id)
        await s.commit()
        await dispatch_events(aggregate)


async def _original_mid_band_rationale(thread_id: UUID, author_external_id: str) -> str | None:
    """Find the developer's wontfix/intentional message that triggered the
    yaaos confirm-request reply.

    Walks the thread chronologically; the most recent human message from
    the same author that came BEFORE yaaos's confirm-request is the
    rationale.
    """
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(CommentMessageRow)
                    .where(CommentMessageRow.thread_id == thread_id)
                    .order_by(CommentMessageRow.created_at)
                )
            )
            .scalars()
            .all()
        )
    last_human_before_confirm: CommentMessageRow | None = None
    for row in rows:
        if row.author_kind == "yaaos" and "reply `confirm`" in (row.body or "").lower():
            # Found the yaaos confirm-request — stop walking.
            break
        if row.author_kind == "human" and row.author_external_id == author_external_id:
            last_human_before_confirm = row
    return last_human_before_confirm.body if last_human_before_confirm else None


# ── verify_fix subflow (plan §6.5) ───────────────────────────────────────────


async def _run_verify_fix(
    *,
    pr_id: UUID,
    org_id: UUID,
    finding_id: UUID,
    thread_id: UUID,
    reply_parent_external_id: str,
) -> None:
    """Provision workspace at HEAD → coding_agent.verify_fix → apply + post."""
    try:
        pr = await pull_requests.get(pr_id, org_id=org_id)
        vcs_plugin = get_vcs_plugin(pr.plugin_id)
        vcs_pr = await vcs_plugin.fetch_pr(pr.external_id)
        async with db_session() as s:
            finding = (
                await s.execute(select(FindingRow).where(FindingRow.id == finding_id))
            ).scalar_one_or_none()
        if finding is None:
            return

        async with with_workspace(
            "in_process",
            WorkspaceSpec(
                repo=RepoRefForSpec(plugin_id=pr.plugin_id, external_id=pr.repo_external_id),
                sha=vcs_pr.head_sha,
                branch_name=vcs_pr.head_branch,
                base_sha=vcs_pr.base_sha,
                base_branch=vcs_pr.base_branch,
                resource_caps=ResourceCaps(),
                network_policy=NetworkPolicy.GITHUB_ONLY,
                org_id=org_id,
            ),
            org_id=org_id,
        ) as ws:
            # Plan §6.5: hand the agent the ORIGINAL code (captured at
            # finding-creation time) AND the current code at the resolved
            # anchor, so it can decide whether the diff actually fixes the
            # flagged issue. `original_lines` lives on `current_anchor` JSONB.
            original_lines_raw = finding.current_anchor.get("original_lines") or []
            original_snippet = "\n".join(original_lines_raw) if original_lines_raw else "(unavailable)"
            anchor_file = finding.current_anchor.get("file_path", "")
            anchor_line_start = int(finding.current_anchor.get("line_start", 1))
            anchor_line_end = int(finding.current_anchor.get("line_end", 1))
            current_text = await ws.read_text(anchor_file)
            if current_text is not None:
                current_lines = current_text.splitlines()
                # Clamp to file bounds (defensive — anchor may point past EOF
                # if the resolve_anchor pass marked it gone before save).
                ls = max(1, min(anchor_line_start, len(current_lines)))
                le = max(ls, min(anchor_line_end, len(current_lines)))
                current_snippet = "\n".join(current_lines[ls - 1 : le])
            else:
                current_snippet = "(file missing at HEAD)"
            ctx = VerifyFixContext(
                original_finding_title=finding.title,
                original_finding_body=finding.body,
                original_rule_id=finding.rule_id,
                original_code_snippet=original_snippet,
                current_code_snippet=current_snippet,
                current_anchor=AgentFindingAnchor(
                    file_path=anchor_file,
                    line_start=anchor_line_start,
                    line_end=anchor_line_end,
                ),
                agent_config={},
            )
            result = await coding_agent.verify_fix(plugin_id="claude_code", workspace=ws, context=ctx)

        if result.status != InvocationStatus.SUCCESS:
            log.warning("verify_fix.agent_failed", finding_id=str(finding_id), status=str(result.status))
            return

        async with db_session() as s:
            await acquire_pr_lock(s, pr_id)
            agg_repo = SqlAlchemyAggregateRepository(s)
            aggregate = await agg_repo.load(pr_id=pr_id, org_id=org_id)
            action = apply_verify_fix_result(
                aggregate,
                finding_id=finding_id,
                still_present=result.still_present,
                confidence=result.confidence,
                observed_line=result.observed_line,
            )
            if action.reply_body:
                try:
                    yaaos_comment_id = await vcs_plugin.post_comment_reply(
                        pr.external_id, reply_parent_external_id, action.reply_body
                    )
                except Exception:
                    log.exception("verify_fix.post_reply_failed", finding_id=str(finding_id))
                    yaaos_comment_id = f"local-verify-{finding_id}"
                aggregate.append_message(
                    thread_id=thread_id,
                    author_kind="yaaos",
                    author_external_id=_REVIEWER_TAG,
                    external_comment_id=yaaos_comment_id,
                    body=action.reply_body,
                    in_reply_to_external_id=reply_parent_external_id,
                )
            await agg_repo.save(aggregate)
            await dispatch_audits(aggregate, session=s, actor=Actor.system(), org_id=org_id)
            await s.commit()
            await dispatch_events(aggregate)

    except Exception:
        log.exception("verify_fix.crashed", finding_id=str(finding_id))


# ── answer_question subflow ──────────────────────────────────────────────────


async def _prior_messages_for_answer(thread_id: UUID) -> list[PriorThreadMessage]:
    """Load the full thread in chronological order for the answer-question prompt.

    The classifier's prior-messages helper trims for token budget; here we
    pass the entire conversation so the agent can read context across N
    back-and-forth turns. POC scale → no truncation needed.
    """
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(CommentMessageRow)
                    .where(CommentMessageRow.thread_id == thread_id)
                    .order_by(CommentMessageRow.created_at)
                )
            )
            .scalars()
            .all()
        )
    return [PriorThreadMessage(author_kind=r.author_kind, body=r.body) for r in rows]


async def _run_answer_question(
    *,
    pr_id: UUID,
    org_id: UUID,
    finding_id: UUID,
    thread_id: UUID,
    reply_parent_external_id: str,
    question: str,
) -> None:
    """Provision workspace at HEAD → coding_agent.answer_question → post reply.

    Mirrors `_run_verify_fix` but emits a free-text answer rather than a
    verdict — no aggregate state transition, just append the yaaos reply to
    the thread and commit.
    """
    try:
        pr = await pull_requests.get(pr_id, org_id=org_id)
        vcs_plugin = get_vcs_plugin(pr.plugin_id)
        vcs_pr = await vcs_plugin.fetch_pr(pr.external_id)
        async with db_session() as s:
            finding = (
                await s.execute(select(FindingRow).where(FindingRow.id == finding_id))
            ).scalar_one_or_none()
        if finding is None:
            return

        prior_thread = await _prior_messages_for_answer(thread_id)

        async with with_workspace(
            "in_process",
            WorkspaceSpec(
                repo=RepoRefForSpec(plugin_id=pr.plugin_id, external_id=pr.repo_external_id),
                sha=vcs_pr.head_sha,
                branch_name=vcs_pr.head_branch,
                base_sha=vcs_pr.base_sha,
                base_branch=vcs_pr.base_branch,
                resource_caps=ResourceCaps(),
                network_policy=NetworkPolicy.GITHUB_ONLY,
                org_id=org_id,
            ),
            org_id=org_id,
        ) as ws:
            anchor_file = finding.current_anchor.get("file_path", "")
            anchor_line_start = int(finding.current_anchor.get("line_start", 1))
            anchor_line_end = int(finding.current_anchor.get("line_end", 1))
            current_text = await ws.read_text(anchor_file)
            if current_text is not None:
                current_lines = current_text.splitlines()
                ls = max(1, min(anchor_line_start, len(current_lines)))
                le = max(ls, min(anchor_line_end, len(current_lines)))
                code_snippet = "\n".join(current_lines[ls - 1 : le])
            else:
                code_snippet = "(file missing at HEAD)"
            ctx = AnswerQuestionContext(
                original_finding_title=finding.title,
                original_finding_body=finding.body,
                original_rule_id=finding.rule_id,
                code_snippet=code_snippet,
                current_anchor=AgentFindingAnchor(
                    file_path=anchor_file,
                    line_start=anchor_line_start,
                    line_end=anchor_line_end,
                ),
                question=question,
                prior_messages=prior_thread,
                base_sha=vcs_pr.base_sha,
                head_sha=vcs_pr.head_sha,
                agent_config={},
            )
            result = await coding_agent.answer_question(plugin_id="claude_code", workspace=ws, context=ctx)

        if result.status != InvocationStatus.SUCCESS or not result.answer.strip():
            log.warning(
                "answer_question.agent_failed",
                finding_id=str(finding_id),
                status=str(result.status),
                error=result.error_message,
            )
            return

        async with db_session() as s:
            await acquire_pr_lock(s, pr_id)
            agg_repo = SqlAlchemyAggregateRepository(s)
            aggregate = await agg_repo.load(pr_id=pr_id, org_id=org_id)
            try:
                yaaos_comment_id = await vcs_plugin.post_comment_reply(
                    pr.external_id, reply_parent_external_id, result.answer
                )
            except Exception:
                log.exception("answer_question.post_reply_failed", finding_id=str(finding_id))
                yaaos_comment_id = f"local-answer-{finding_id}"
            aggregate.append_message(
                thread_id=thread_id,
                author_kind="yaaos",
                author_external_id=_REVIEWER_TAG,
                external_comment_id=yaaos_comment_id,
                body=result.answer,
                in_reply_to_external_id=reply_parent_external_id,
            )
            await agg_repo.save(aggregate)
            await dispatch_audits(aggregate, session=s, actor=Actor.system(), org_id=org_id)
            await s.commit()
            await dispatch_events(aggregate)

    except Exception:
        log.exception("answer_question.crashed", finding_id=str(finding_id))


__all__ = ["handle_developer_reply"]
