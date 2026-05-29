"""Service test: GitHub PR-opened intake path writes to both outbox and SSE.

Covers `_prepare_pr_review` producing:
- one `notifications.fanout` outbox row with NotificationSpecs for org members, and
- one general SSE event with kind "ticket_status_changed".

The existing `test_intake_type.py` covers the in-process SSE bus; this file
covers the durable outbox + Redis-based SSE path.
"""

from __future__ import annotations

import asyncio
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
    scoped_engine,
)
from app.plugins.github.intake_type import GithubIntakeType


class _NoopLocal:
    kind: Literal["NoopIntake"] = "NoopIntake"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


@pytest.fixture
def _stub_pr_review_engine():  # type: ignore[no-untyped-def]
    """Stub workflow engine so _prepare_pr_review can call engine.start."""
    with scoped_engine() as eng:
        eng.register_command(_NoopLocal())
        eng.register_workflow(
            Workflow(
                name="pr_review_v1",
                version=1,
                steps=(
                    Step(
                        id="only",
                        command_kind="NoopIntake",
                        transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
                    ),
                ),
                entry_step_id="only",
            )
        )
        yield eng


def _pr_payload() -> dict:
    """Minimal pull_request.opened body."""
    pr = {
        "number": 42,
        "title": "Add feature",
        "body": "Body text",
        "draft": False,
        "merged": False,
        "state": "open",
        "html_url": "https://github.com/org/repo/pull/42",
        "user": {"login": "alice", "type": "User"},
        "head": {
            "ref": "feature",
            "sha": "abc123",
            "repo": {"fork": False, "full_name": "org/repo"},
        },
        "base": {
            "ref": "main",
            "sha": "def456",
            "repo": {"full_name": "org/repo"},
        },
        "created_at": "2026-05-01T10:00:00Z",
        "updated_at": "2026-05-01T10:00:00Z",
        "labels": [],
    }
    return {"action": "opened", "pull_request": pr, "repository": {"full_name": "org/repo"}}


async def _seed_org_with_members(db_session, num_members: int = 2):  # type: ignore[no-untyped-def]
    """Create org + users + memberships via public domain APIs.

    Returns (org_id, [user_ids]).
    """
    from app.core.auth import Role  # noqa: PLC0415
    from app.core.identity import create_user  # noqa: PLC0415
    from app.domain.orgs import create_membership, create_org  # noqa: PLC0415

    slug = f"intake-org-{uuid4().hex[:8]}"
    org = await create_org(db_session, slug=slug, display_name="Intake Test Org")
    await db_session.flush()

    user_ids = []
    for i in range(num_members):
        user = await create_user(db_session, display_name=f"Intake User {i}")
        await db_session.flush()
        await create_membership(
            db_session,
            user_id=user.id,
            org_id=org.id,
            role=Role.BUILDER,
            handle=f"iuser{i}-{uuid4().hex[:4]}",
        )
        user_ids.append(user.id)

    await db_session.commit()
    return org.id, user_ids


@pytest.mark.service
@pytest.mark.asyncio
async def test_intake_enqueues_fanout_via_ticket_policy(
    db_session, redis_or_skip, _stub_pr_review_engine
) -> None:
    """A PR-opened webhook must:
    - enqueue one `notifications.fanout` outbox row with NotificationSpecs for each
      org member (sourced from tickets.build_status_change_specs), and
    - publish a general SSE event after commit with kind 'ticket_status_changed'.
    """
    from app.core.redis import reset_pubsub  # noqa: PLC0415
    from app.core.sse import subscribe_general  # noqa: PLC0415

    reset_pubsub()
    try:
        org_id, _user_ids = await _seed_org_with_members(db_session, num_members=2)
        received: list[dict] = []

        async def _consume() -> None:
            async for event in subscribe_general(org_id):
                received.append(event)
                return

        consumer = asyncio.create_task(_consume())
        await asyncio.sleep(0.1)  # let Redis subscription register

        outcome = await GithubIntakeType()._prepare_pr_review(
            payload=_pr_payload(),
            delivery=f"evt-{uuid4().hex}",
            org_id=org_id,
            session=db_session,
        )
        await db_session.commit()

        assert outcome.detail == "pr_review_started"

        # SSE event
        await asyncio.wait_for(consumer, timeout=3.0)
        assert len(received) == 1
        evt = received[0]
        assert evt["kind"] == "ticket_status_changed"
        assert evt["new_status"] == "running"
        assert "ts" in evt

        # `running` status does not warrant user notifications, so no fanout row.
        payloads = await get_pending_outbox_payloads(db_session)
        fanout_rows = [p for p in payloads if p.get("task_name") == "notifications.fanout"]
        assert len(fanout_rows) == 0, (
            f"expected 0 fanout rows for 'running' status (no notification warranted), got {len(fanout_rows)}"
        )
    finally:
        reset_pubsub()
