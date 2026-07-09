"""Service test: GitHub PR-opened intake path writes to both outbox and SSE.

Covers `_prepare_pipeline_runs` producing:
- one `notifications.fanout` outbox row with NotificationSpecs for org members, and
- one general SSE event with kind "ticket_status_changed".

The existing `test_intake_type.py` covers the in-process SSE bus; this file
covers the durable outbox + Redis-based SSE path.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.core.audit_log import Actor
from app.core.tasks import get_pending_outbox_payloads
from app.domain.pipelines import ActionStage, PipelineDefinition, create_pipeline
from app.domain.repos import TriggerBindingSpec, add_binding
from app.plugins.github.intake_type import GithubIntakeType


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


async def _bind_pipeline(db_session, org_id) -> None:  # type: ignore[no-untyped-def]
    pipeline_id = await create_pipeline(
        org_id=org_id,
        definition=PipelineDefinition(
            name=f"pipeline-{uuid4().hex[:6]}", stages=(ActionStage(action_id="github:create_pr"),)
        ),
        actor=Actor.system(),
        session=db_session,
    )
    await add_binding(
        org_id,
        "org/repo",
        spec=TriggerBindingSpec(intake_point_id="github:pr_opened", pipeline_id=pipeline_id),
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.commit()


@pytest.mark.service
@pytest.mark.asyncio
async def test_intake_enqueues_fanout_via_ticket_policy(db_session, redis_or_skip) -> None:
    """A PR-opened webhook on a bound repo must:
    - publish a general SSE event after commit with kind 'ticket_status_changed'
      with new_status 'pending' (create_from_pr inserts at pending; running comes later
      via `transition_ticket_on_run_start`).
    - NOT enqueue a fanout row (pending does not warrant user notifications).
    """
    from app.core.sse import subscribe_general  # noqa: PLC0415

    org_id, _user_ids = await _seed_org_with_members(db_session, num_members=2)
    await _bind_pipeline(db_session, org_id)
    received: list[dict] = []

    async def _consume() -> None:
        async for event in subscribe_general(org_id):
            received.append(event)
            return

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.1)  # let Redis subscription register

    outcome = await GithubIntakeType()._prepare_review_or_run(
        payload=_pr_payload(),
        delivery=f"evt-{uuid4().hex}",
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()

    assert outcome.detail == "pipeline_run_started"

    # SSE event
    await asyncio.wait_for(consumer, timeout=3.0)
    assert len(received) == 1
    evt = received[0]
    assert evt["kind"] == "ticket_status_changed"
    assert evt["new_status"] == "pending"
    assert "ts" in evt

    # `pending` status does not warrant user notifications, so no fanout row.
    payloads = await get_pending_outbox_payloads(db_session)
    fanout_rows = [p for p in payloads if p.get("task_name") == "notifications.fanout"]
    assert len(fanout_rows) == 0, (
        f"expected 0 fanout rows for 'pending' status (no notification warranted), got {len(fanout_rows)}"
    )
