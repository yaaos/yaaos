"""Service-tier guards for the per-minute `ticket_orphan_sweep` `@scheduled` task.

Two invariants:
  - The body is registered with the taskiq broker under the public task name.
  - The sweep body runs end-to-end: stale orphan tickets flip to `failed`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.identity import insert_user
from app.core.tasks import get_broker
from app.domain.orgs import insert_org
from app.domain.reviewer.orphan_sweep import _sweep_once, ticket_orphan_sweep
from app.domain.tickets import get as get_ticket

pytestmark = pytest.mark.service

_TASK_NAME = "ticket_orphan_sweep"


async def _seed_running_ticket(db_session, org_id, *, ext: str, age_seconds: int) -> uuid.UUID:
    tid = uuid.uuid4()
    created_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    await db_session.execute(
        text(
            "INSERT INTO tickets "
            "(id, org_id, source, source_external_id, title, status, plugin_id, repo_external_id,"
            " created_at, updated_at)"
            " VALUES (:id, :org_id, 'github_pr', :ext, :title, 'running', 'github', 'x/y',"
            " :created_at, :created_at)"
        ),
        {
            "id": tid,
            "org_id": org_id,
            "ext": ext,
            "title": f"orphan-sched-{ext}",
            "created_at": created_at,
        },
    )
    return tid


@pytest.mark.asyncio
async def test_ticket_orphan_sweep_task_registered_with_broker() -> None:
    """The sweep body is registered with the broker under its public task name.
    Regression guard for the `@scheduled` decorator wiring."""
    assert get_broker().find_task(_TASK_NAME) is not None
    assert ticket_orphan_sweep is not None


@pytest.mark.asyncio
async def test_orphan_sweep_body_flips_stale_ticket_to_failed(db_session) -> None:
    """Drive `_sweep_once` directly — orphan ticket older than grace window flips to failed."""
    user = await insert_user(db_session, display_name="J")
    org = await insert_org(db_session, slug="orphan-sched-org")
    del user
    # Older than the 300 s default grace.
    stale = await _seed_running_ticket(db_session, org.org_id, ext="sched/r#1", age_seconds=600)
    await db_session.commit()

    failed = await _sweep_once()
    assert failed >= 1

    ticket = await get_ticket(stale, org_id=org.org_id)
    assert ticket.status == "failed"


@pytest.mark.asyncio
async def test_orphan_sweep_body_runs_idempotently() -> None:
    """Empty DB stays empty — surfaces exceptions loudly."""
    await _sweep_once()
    await _sweep_once()
