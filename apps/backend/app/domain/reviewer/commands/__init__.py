"""Reviewer WorkflowCommands for the five M05 task modes.

Five **Workspace** commands wrap `domain/coding_agent` invocations against
a workspace:
- `CodeReview` — full-PR review.
- `IncrementalReview` — push-driven incremental review against a base sha.
- `VerifyFix` — ack a developer's "is this fixed?" reply on a finding.
- `StaleCheck` — periodic check that an open finding still applies.
- `AnswerQuestion` — answer a developer @yaaos-mention on a finding.

Five **Local** commands handle the control-plane side:
- `CheckShouldReview` — admission gating (draft/skip-label/external-contrib/
  org-config) before any workspace is provisioned.
- `PostFindings` — admit findings via the aggregate, post to GitHub.
- `ResolveFinding` — close a finding's thread on a verified fix.
- `ArchiveStaleFindings` — mark stale findings archived.
- `PostReply` — post a reply on a finding's thread.

`CheckShouldReview` ships with a real body that reads admission signals
(is_draft / is_fork / labels) from the ticket payload. The other four Local
commands and all five Workspace commands ship as stubs pending the queue.py
dismantle that wires the existing reviewer pipeline through them.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import structlog

from app.core.database import session as db_session
from app.core.workflow import CommandCategory, CommandContext, Outcome
from app.core.workspace import (
    Workspace,
    WorkspaceTicketContext,
    get_workflow_context_provider,
    get_workspace,
)
from app.domain.tickets import get_payload as get_ticket_payload

log = structlog.get_logger("domain.reviewer.commands")

# Labels whose presence on a PR force-skips the review. Matches the legacy
# `queue.py` behavior so the cutover is a straight swap. Case-insensitive.
SKIP_LABELS: frozenset[str] = frozenset({"yaaos-skip", "no-review", "wip"})

# ── Workspace commands (5) ──────────────────────────────────────────────


class _WorkspaceReviewCommand:
    """Workspace-category reviewer command. The base does three things on
    every invocation:

    1. Reads `workspace_id` from inputs and resolves it to a live `Workspace`
       handle via `core/workspace.get_workspace()`. Missing or unresolved →
       `Outcome.failure` (the upstream `ProvisionWorkspace` step would have
       failed to write `workspace_id` into outputs, or the row was destroyed
       between provision and review).
    2. Fetches the `WorkspaceTicketContext` (org_id, plugin_id, repo,
       payload, pr_id) for the workflow's ticket via the registered
       `WorkflowContextProvider`. Missing provider or missing ticket → also
       `Outcome.failure` — the workflow can't proceed without org context
       to look up plugins / build review context.
    3. Hands the resolved (workspace, ticket_ctx, inputs, ctx) to the
       subclass via `_run_in_workspace(...)`.

    Subclass bodies (`CodeReview`, `IncrementalReview`, `VerifyFix`,
    `StaleCheck`, `AnswerQuestion`) override `_run_in_workspace` to build
    their `domain/coding_agent` context and invoke the matching agent
    method. Phase 4 ships the substrate; the per-command bodies land
    incrementally as their `<Foo>Context` builders are extracted from
    `queue.py`.
    """

    category = CommandCategory.WORKSPACE
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        ws_id_raw = inputs.get("workspace_id")
        if not ws_id_raw:
            return Outcome.failure(reason="missing workspace_id input")
        try:
            ws_id = UUID(str(ws_id_raw))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid workspace_id: {ws_id_raw!r}")

        workspace = await get_workspace(ws_id)
        if workspace is None:
            return Outcome.failure(reason=f"workspace {ws_id} not resolvable")

        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")

        try:
            ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        except Exception as exc:
            log.exception(
                "workspace_review.context_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        if ticket_ctx is None:
            return Outcome.failure(reason=f"ticket {ctx.ticket_id} not found")

        return await self._run_in_workspace(workspace, ticket_ctx, inputs, ctx)

    async def _run_in_workspace(
        self,
        workspace: Workspace,
        ticket_ctx: WorkspaceTicketContext,
        inputs: dict[str, Any],
        ctx: CommandContext,
    ) -> Outcome:
        """Override in subclasses to invoke the relevant `domain/coding_agent`
        method against the live workspace. Default body is success — keeps
        the engine workflow draining cleanly until each real body lands."""
        del workspace, ticket_ctx, inputs, ctx
        return Outcome.success()


class CodeReview(_WorkspaceReviewCommand):
    kind = "CodeReview"


class IncrementalReview(_WorkspaceReviewCommand):
    kind = "IncrementalReview"


class VerifyFix(_WorkspaceReviewCommand):
    kind = "VerifyFix"


class StaleCheck(_WorkspaceReviewCommand):
    kind = "StaleCheck"


class AnswerQuestion(_WorkspaceReviewCommand):
    kind = "AnswerQuestion"


# ── Local commands (5) ──────────────────────────────────────────────────


class _LocalReviewCommand:
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


class CheckShouldReview:
    """Admission gate before provisioning. Returns `Outcome.success(label='skip')`
    when the PR is draft / fork / bot-authored / skip-labelled; workflow
    then terminates without spinning up a workspace. The PR payload (set by
    `plugins/github/intake_type`) carries `is_draft`, `is_fork`, `labels`,
    `author_login`."""

    kind = "CheckShouldReview"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs
        async with db_session() as s:
            payload = await get_ticket_payload(UUID(ctx.ticket_id), session=s)

        reason = _decide_skip(payload)
        if reason is not None:
            log.info(
                "checkshouldreview.skip",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
                reason=reason,
            )
            return Outcome.success(label="skip", outputs={"reason": reason})

        return Outcome.success(outputs={"pr_external_id": payload.get("pr_external_id")})


def _decide_skip(payload: dict[str, Any]) -> str | None:
    """First-match-wins admission. Returns a skip reason string or None for go."""
    if payload.get("is_draft"):
        return "draft"
    if payload.get("is_fork"):
        return "fork"
    labels = {str(label).lower() for label in (payload.get("labels") or [])}
    forced = labels & {label.lower() for label in SKIP_LABELS}
    if forced:
        return f"label:{sorted(forced)[0]}"
    author = (payload.get("author_login") or "").lower()
    if author.endswith("[bot]") or author.endswith("-bot"):
        return "bot_author"
    return None


class PostFindings(_LocalReviewCommand):
    """Persist coding-agent findings through the admission pipeline.

    Inputs:
    - `draft_findings`: list of `FindingDraft`-shaped dicts (as the
      upstream `CodeReview` Workspace step will emit them).
    - `workspace_id`: the workspace the drafts were produced against.
      Needed because `findingdrafts_to_raw` reads anchored file content
      from the workspace to build stable fingerprints.

    Flow: deserialize FindingDrafts → read referenced files from the
    workspace → `findingdrafts_to_raw` → `admit_raw_findings`. Outputs
    `admitted_count` + `dropped_count` so audit consumers can see the
    admission ratio.

    Defensive: empty/missing `draft_findings` → success-no-op (a stubbed
    upstream review or a successful review with nothing to report). Missing
    workspace or ticket context → failure. Bad FindingDraft schema in any
    item → failure (caller should not have produced it).

    NOT included yet: posting admitted findings to GitHub via
    `vcs.post_review`. That's a separate slice that wires the vcs plugin
    lookup + thread bookkeeping; admitted findings persist in the aggregate
    today and become visible to future review runs.
    """

    kind = "PostFindings"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        drafts_raw = inputs.get("draft_findings") or []
        if not drafts_raw:
            return Outcome.success(outputs={"admitted_count": 0, "dropped_count": 0})

        ws_id_raw = inputs.get("workspace_id")
        if not ws_id_raw:
            return Outcome.failure(reason="missing workspace_id input")
        try:
            ws_id = UUID(str(ws_id_raw))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid workspace_id: {ws_id_raw!r}")

        workspace = await get_workspace(ws_id)
        if workspace is None:
            return Outcome.failure(reason=f"workspace {ws_id} not resolvable")

        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")

        ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        if ticket_ctx is None or ticket_ctx.pr_id is None:
            log.info(
                "post_findings.no_pr_link",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.success(outputs={"admitted_count": 0, "dropped_count": 0})

        # Deferred imports — keep the commands module-import cheap for the
        # engine registration path.
        from app.domain.coding_agent import FindingDraft  # noqa: PLC0415
        from app.domain.reviewer.admission import (  # noqa: PLC0415
            admit_raw_findings,
            findingdrafts_to_raw,
        )

        # 1. Deserialize FindingDrafts.
        try:
            drafts = [FindingDraft.model_validate(d) for d in drafts_raw]
        except Exception as exc:
            return Outcome.failure(reason=f"invalid FindingDraft payload: {exc}")

        # 2. Pre-fetch file contents for each referenced anchor file. Using a
        # dict.get matches the sync read_file signature `findingdrafts_to_raw`
        # expects; the workspace's own read is async.
        file_contents: dict[str, list[str] | None] = {}
        for draft in drafts:
            path = draft.anchor.file_path
            if path in file_contents:
                continue
            text = await workspace.read_text(path)
            file_contents[path] = text.splitlines() if text else None

        commit_sha = str(ticket_ctx.payload.get("head_sha") or "")
        raw = findingdrafts_to_raw(
            drafts,
            commit_sha=commit_sha,
            read_file=file_contents.get,
        )

        # 3. Persist via admission. workflow_execution_id doubles as the
        # review_id for observation tracking until the queue.py dismantle
        # replaces it with a proper Review row.
        async with db_session() as s:
            result = await admit_raw_findings(
                pr_id=ticket_ctx.pr_id,
                org_id=ticket_ctx.org_id,
                review_id=UUID(ctx.workflow_execution_id),
                raw=raw,
                session=s,
            )
            await s.commit()

        log.info(
            "post_findings.done",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
            drafts_in=len(drafts_raw),
            drafts_after_read=len(raw),
            admitted=len(result.admitted),
            dropped=len(result.drops),
        )
        return Outcome.success(
            outputs={
                "admitted_count": len(result.admitted),
                "dropped_count": len(result.drops),
            }
        )


class ResolveFinding(_LocalReviewCommand):
    """Apply a verify-fix verdict to a single finding. Receives `verdict`
    from inputs (sourced from the prior `VerifyFix` Workspace step via
    `$verify.verdict`). The verdict shape mirrors `coding_agent.VerifyFixResult`:

        {"finding_id": "<uuid>", "still_present": <bool>, "confidence": <float>}

    Calls `aggregate.record_fix_verification(...)` which transitions the
    finding to `RESOLVED_CONFIRMED` iff `still_present=False` AND
    `confidence ≥ threshold` (default 0.80). Lower-confidence verdicts and
    `still_present=True` are no-ops — the finding stays open and the
    workflow ends cleanly.

    Defensive: empty/missing verdict → success-no-op. Missing pr_id link →
    success-no-op. Unknown finding_id → skipped, not failed.

    Outputs:
    - `transitioned_to`: the new state (string) if a transition fired, else None
    """

    kind = "ResolveFinding"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        verdict = inputs.get("verdict") or {}
        if not isinstance(verdict, dict) or not verdict:
            return Outcome.success(outputs={"transitioned_to": None})

        finding_id_raw = verdict.get("finding_id")
        if not finding_id_raw:
            return Outcome.success(outputs={"transitioned_to": None})
        try:
            finding_id = UUID(str(finding_id_raw))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid finding_id: {finding_id_raw!r}")

        still_present = bool(verdict.get("still_present", True))
        try:
            confidence = float(verdict.get("confidence", 0.0))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid confidence: {verdict.get('confidence')!r}")

        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")

        try:
            ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        except Exception as exc:
            log.exception(
                "resolve_finding.context_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        if ticket_ctx is None or ticket_ctx.pr_id is None:
            log.info(
                "resolve_finding.no_pr_link",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.success(outputs={"transitioned_to": None})

        from app.domain.reviewer.repository import (  # noqa: PLC0415
            SqlAlchemyAggregateRepository,
        )

        async with db_session() as s:
            repo = SqlAlchemyAggregateRepository(s)
            aggregate = await repo.load(pr_id=ticket_ctx.pr_id, org_id=ticket_ctx.org_id)
            known_ids = {f.id for f in aggregate.findings}
            if finding_id not in known_ids:
                log.info(
                    "resolve_finding.unknown_finding",
                    workflow_execution_id=ctx.workflow_execution_id,
                    finding_id=str(finding_id),
                )
                return Outcome.success(outputs={"transitioned_to": None})

            new_state = aggregate.record_fix_verification(
                finding_id=finding_id,
                still_present=still_present,
                confidence=confidence,
            )
            await repo.save(aggregate)
            await s.commit()

        log.info(
            "resolve_finding.done",
            workflow_execution_id=ctx.workflow_execution_id,
            finding_id=str(finding_id),
            still_present=still_present,
            confidence=confidence,
            transitioned_to=(new_state.value if new_state is not None else None),
        )
        return Outcome.success(
            outputs={"transitioned_to": new_state.value if new_state is not None else None}
        )


class ArchiveStaleFindings(_LocalReviewCommand):
    """Mark a list of findings as `STALE` in the reviewer aggregate. Receives
    `stale_finding_ids: list[str]` from inputs — typically sourced from the
    prior `StaleCheck` Workspace step via `$check.stale_finding_ids`.

    Idempotent and defensive:
    - Empty / missing input → success-no-op.
    - Ticket with no `pr_id` (intake created the ticket before PR
      materialization) → success-no-op. Nothing to archive.
    - Individual `finding_id` not present in the aggregate → skipped, not a
      failure. The aggregate enumerates findings owned by this PR; ids from
      a stale upstream payload (e.g. finding was hard-deleted) shouldn't
      sink the whole step.

    Outputs `archived_count` so downstream steps or audits can read how
    many state transitions actually fired.
    """

    kind = "ArchiveStaleFindings"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        stale_ids_raw = inputs.get("stale_finding_ids") or []
        if not stale_ids_raw:
            return Outcome.success(outputs={"archived_count": 0})

        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")

        try:
            ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        except Exception as exc:
            log.exception(
                "archive_stale_findings.context_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        if ticket_ctx is None or ticket_ctx.pr_id is None:
            log.info(
                "archive_stale_findings.no_pr_link",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.success(outputs={"archived_count": 0})

        # Defer the heavy SqlAlchemyAggregateRepository import — keeps the
        # commands module-import cheap for the engine registration path.
        from app.domain.reviewer.repository import (  # noqa: PLC0415
            SqlAlchemyAggregateRepository,
        )

        archived = 0
        skipped = 0
        async with db_session() as s:
            repo = SqlAlchemyAggregateRepository(s)
            aggregate = await repo.load(pr_id=ticket_ctx.pr_id, org_id=ticket_ctx.org_id)
            known_ids = {f.id for f in aggregate.findings}
            for raw_id in stale_ids_raw:
                try:
                    fid = UUID(str(raw_id))
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                if fid not in known_ids:
                    skipped += 1
                    continue
                # confidence=1.0 because StaleCheck has already decided;
                # ArchiveStaleFindings is the durable persistence step.
                result = aggregate.record_stale_detection(finding_id=fid, still_applies=False, confidence=1.0)
                if result is not None:
                    archived += 1
            await repo.save(aggregate)
            await s.commit()

        log.info(
            "archive_stale_findings.done",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
            archived=archived,
            skipped=skipped,
        )
        return Outcome.success(outputs={"archived_count": archived, "skipped_count": skipped})


class PostReply(_LocalReviewCommand):
    """Append a yaaos-authored reply to a finding's comment thread.

    Inputs:
    - `reply_body`: text of the reply (from the prior `AnswerQuestion`
      Workspace step).
    - `finding_id`: which finding's thread to reply on (from
      `$ticket.finding_id`).

    Loads the reviewer aggregate by `pr_id`, finds the thread for
    `finding_id`, appends a `CommentMessage` with `author_kind="yaaos"`.
    The external_comment_id is set to a `local-<uuid>` placeholder — the
    GitHub-side post (vcs.post_comment_reply) is a follow-on slice; today
    the reply persists locally but doesn't appear on GitHub yet.

    Defensive: empty/missing inputs → success-no-op (workflow drain).
    Missing pr_id, unknown finding, or no existing thread → success-no-op
    with a log line (the reply has nothing to attach to). Real errors
    (provider missing) return failure.
    """

    kind = "PostReply"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        reply_body = inputs.get("reply_body")
        finding_id_raw = inputs.get("finding_id")
        if not reply_body or not finding_id_raw:
            return Outcome.success(outputs={"posted": False, "reason": "empty_input"})

        try:
            finding_id = UUID(str(finding_id_raw))
        except (TypeError, ValueError):
            return Outcome.failure(reason=f"invalid finding_id: {finding_id_raw!r}")

        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")

        ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        if ticket_ctx is None or ticket_ctx.pr_id is None:
            log.info(
                "post_reply.no_pr_link",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.success(outputs={"posted": False, "reason": "no_pr_link"})

        # Deferred import — heavy module.
        from app.domain.reviewer.repository import (  # noqa: PLC0415
            SqlAlchemyAggregateRepository,
        )

        async with db_session() as s:
            repo = SqlAlchemyAggregateRepository(s)
            aggregate = await repo.load(pr_id=ticket_ctx.pr_id, org_id=ticket_ctx.org_id)
            known_ids = {f.id for f in aggregate.findings}
            if finding_id not in known_ids:
                log.info(
                    "post_reply.unknown_finding",
                    workflow_execution_id=ctx.workflow_execution_id,
                    finding_id=str(finding_id),
                )
                return Outcome.success(outputs={"posted": False, "reason": "unknown_finding"})

            thread = aggregate.thread_for_finding(finding_id)
            if thread is None:
                log.info(
                    "post_reply.no_thread",
                    workflow_execution_id=ctx.workflow_execution_id,
                    finding_id=str(finding_id),
                )
                return Outcome.success(outputs={"posted": False, "reason": "no_thread"})

            # Placeholder external id — GitHub post lands in the follow-on
            # slice. Matches the existing local-<id> convention from queue.py.
            placeholder_external_id = f"local-reply-{uuid4()}"
            aggregate.append_message(
                thread_id=thread.id,
                author_kind="yaaos",
                author_external_id="yaaos",
                external_comment_id=placeholder_external_id,
                body=str(reply_body),
            )
            await repo.save(aggregate)
            await s.commit()

        log.info(
            "post_reply.persisted",
            workflow_execution_id=ctx.workflow_execution_id,
            finding_id=str(finding_id),
            thread_id=str(thread.id),
        )
        return Outcome.success(
            outputs={
                "posted": True,
                "thread_id": str(thread.id),
                "external_comment_id": placeholder_external_id,
            }
        )


ALL_WORKSPACE_COMMANDS: tuple[_WorkspaceReviewCommand, ...] = (
    CodeReview(),
    IncrementalReview(),
    VerifyFix(),
    StaleCheck(),
    AnswerQuestion(),
)

ALL_LOCAL_COMMANDS: tuple[object, ...] = (
    CheckShouldReview(),
    PostFindings(),
    ResolveFinding(),
    ArchiveStaleFindings(),
    PostReply(),
)


__all__ = [
    "ALL_LOCAL_COMMANDS",
    "ALL_WORKSPACE_COMMANDS",
    "AnswerQuestion",
    "ArchiveStaleFindings",
    "CheckShouldReview",
    "CodeReview",
    "IncrementalReview",
    "PostFindings",
    "PostReply",
    "ResolveFinding",
    "StaleCheck",
    "VerifyFix",
]
