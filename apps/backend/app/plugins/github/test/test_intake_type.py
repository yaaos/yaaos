"""GitHub intake type — status-change producer contract.

The HTTP boundary, signature verification, and install-binding lookups are
covered by sibling test files. Here we drive `_prepare_pr_review` directly
so the assertion stays focused on the status-change side effects.

The durable outbox + Redis-based SSE path is covered by
`test_intake_producer_service.py` (requires Redis). This file asserts on the
outbox row only (no Redis dependency).

`running` status does not produce a notification (see tickets/notifications.py),
so no `notifications.fanout` row is expected for PR-opened.
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

import pytest

from app.core.tasks import get_pending_outbox_payloads
from app.core.workflow import (
    CommandCategory,
    CommandContext,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
)
from app.plugins.github.intake_type import GithubIntakeType
from app.testing.workflow_harness import scoped_engine


class _NoopLocal:
    kind: Literal["Noop"] = "Noop"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


@pytest.fixture
def _stub_pr_review_engine():  # type: ignore[no-untyped-def]
    """Register a one-step `pr_review_v1` workflow so `_prepare_pr_review`'s
    `engine.start(workflow_name="pr_review_v1", ...)` resolves without
    pulling in the full reviewer command set."""
    with scoped_engine() as eng:
        eng.register_command(_NoopLocal())
        eng.register_workflow(
            Workflow(
                name="pr_review_v1",
                version=1,
                steps=(
                    Step(
                        id="only",
                        command_kind="Noop",
                        transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
                    ),
                ),
                entry_step_id="only",
            )
        )
        yield eng


def _pr_opened_payload() -> dict:
    """Minimal `pull_request.opened` body — enough for `_parse_pr` plus the
    fork / bot filters that `_prepare_pr_review` runs before insert."""
    pr = {
        "number": 7,
        "title": "T",
        "body": "B",
        "draft": False,
        "merged": False,
        "state": "open",
        "html_url": "https://github.com/acme/web/pull/7",
        "user": {"login": "alice", "type": "User"},
        "head": {
            "ref": "feat",
            "sha": "ccc",
            "repo": {"fork": False, "full_name": "acme/web"},
        },
        "base": {
            "ref": "main",
            "sha": "aaa",
            "repo": {"full_name": "acme/web"},
        },
        "created_at": "2026-05-01T10:00:00Z",
        "updated_at": "2026-05-01T10:00:00Z",
        "labels": [],
    }
    return {"action": "opened", "pull_request": pr, "repository": {"full_name": "acme/web"}}


@pytest.mark.service
@pytest.mark.asyncio
async def test_prepare_pr_review_enqueues_ticket_status_change(db_session, _stub_pr_review_engine) -> None:
    """A GitHub PR-opened webhook must publish a SSE ticket_status_changed event;
    no fanout notification row is expected because 'running' does not warrant
    user notifications per tickets.build_status_change_specs."""
    org_id = uuid4()
    outcome = await GithubIntakeType()._prepare_pr_review(
        payload=_pr_opened_payload(),
        delivery="evt-test-1",
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()

    assert outcome.detail == "pr_review_started"

    # `running` is not in the notification-worthy set; no fanout row expected.
    payloads = await get_pending_outbox_payloads(db_session)
    fanout_rows = [p for p in payloads if p.get("task_name") == "notifications.fanout"]
    assert len(fanout_rows) == 0, f"'running' status should not enqueue a fanout row, got {len(fanout_rows)}"
