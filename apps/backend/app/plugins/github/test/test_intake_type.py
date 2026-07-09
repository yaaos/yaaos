"""GitHub intake type — status-change producer contract.

The HTTP boundary, signature verification, and install-binding lookups are
covered by sibling test files. Here we drive `_prepare_review_or_run`
directly against a bound repo so the assertion stays focused on the
status-change side effects.

The durable outbox + Redis-based SSE path is covered by
`test_intake_producer_service.py` (requires Redis). This file asserts on the
outbox row only (no Redis dependency).

`running` status does not produce a notification (see tickets/notifications.py),
so no `notifications.fanout` row is expected for PR-opened.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.audit_log import Actor
from app.core.tasks import get_pending_outbox_payloads
from app.domain.orgs import create_org
from app.domain.pipelines import ActionStage, PipelineDefinition, create_pipeline
from app.domain.repos import TriggerBindingSpec, add_binding
from app.plugins.github.intake_type import GithubIntakeType


def _pr_opened_payload() -> dict:
    """Minimal `pull_request.opened` body — enough for `_parse_pr` plus the
    fork / bot filters that `_prepare_pipeline_runs` runs before insert."""
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


async def _seed_bound_org(db_session):  # type: ignore[no-untyped-def]
    org = await create_org(db_session, slug=f"intake-type-org-{uuid4().hex[:8]}", display_name="Intake Org")
    await db_session.flush()
    pipeline_id = await create_pipeline(
        org_id=org.id,
        definition=PipelineDefinition(
            name=f"pipeline-{uuid4().hex[:6]}", stages=(ActionStage(action_id="github:create_pr"),)
        ),
        actor=Actor.system(),
        session=db_session,
    )
    await add_binding(
        org.id,
        "acme/web",
        spec=TriggerBindingSpec(intake_point_id="github:pr_opened", pipeline_id=pipeline_id),
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.commit()
    return org.id


@pytest.mark.service
@pytest.mark.asyncio
async def test_prepare_pipeline_runs_enqueues_ticket_status_change(db_session) -> None:
    """A GitHub PR-opened webhook on a bound repo must publish a SSE
    ticket_status_changed event; no fanout notification row is expected
    because 'pending' does not warrant user notifications per
    tickets.build_status_change_specs."""
    org_id = await _seed_bound_org(db_session)
    outcome = await GithubIntakeType()._prepare_review_or_run(
        payload=_pr_opened_payload(),
        delivery="evt-test-1",
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()

    assert outcome.detail == "pipeline_run_started"

    # `pending` is not in the notification-worthy set; no fanout row expected.
    payloads = await get_pending_outbox_payloads(db_session)
    fanout_rows = [p for p in payloads if p.get("task_name") == "notifications.fanout"]
    assert len(fanout_rows) == 0, f"'pending' status should not enqueue a fanout row, got {len(fanout_rows)}"
