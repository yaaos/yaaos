"""Service tests for ``app/testing/e2e_setup/service.py``.

Verifies that seed helpers produce the expected durable state and that the
new deliberate side-effect — audit rows emitted by public service calls — is
present after seeding.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.core.audit_log import Actor, ActorKind, list_for_org
from app.core.auth import Role, org_context
from app.domain.orgs import get_membership, get_org_by_slug, get_org_full_by_slug
from app.domain.pipelines import PauseResolution, list_runs_for_ticket, resolve_pause
from app.testing.e2e_setup.service import (
    seed_bootstrap_owner,
    seed_github_install,
    seed_lesson,
    seed_paused_run,
)

# ---------------------------------------------------------------------------
# seed_bootstrap_owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.service
async def test_seed_bootstrap_owner_creates_org_and_membership(db_session) -> None:
    """``seed_bootstrap_owner`` produces an org row + owner membership."""
    ids = await seed_bootstrap_owner(
        email="owner@example.com",
        github_id="gh-42",
        org_slug="seed-test-org",
        display_name="Seed Owner",
    )

    assert ids["org_slug"] == "seed-test-org"

    org = await get_org_by_slug("seed-test-org")
    assert org is not None

    membership = await get_org_full_by_slug(db_session, "seed-test-org")
    assert membership is not None  # org exists — membership verified below via role
    # Verify owner membership via the repository (intra-e2e_setup — testing layer can reach any module).
    from app.core.identity import find_user_by_email  # noqa: PLC0415

    user = await find_user_by_email(db_session, "owner@example.com")
    assert user is not None
    m = await get_membership(db_session, user_id=user.id, org_id=org.id)
    assert m is not None
    assert Role(m.role) == Role.OWNER


@pytest.mark.asyncio
@pytest.mark.service
async def test_seed_bootstrap_owner_emits_audit_rows(db_session) -> None:
    """``seed_bootstrap_owner`` emits ``org.created`` and ``membership.created`` audit rows."""
    await seed_bootstrap_owner(
        email="auditor@example.com",
        github_id="gh-99",
        org_slug="seed-audit-org",
        display_name="Audit Owner",
    )

    org = await get_org_by_slug("seed-audit-org")
    assert org is not None

    all_audit_rows = await list_for_org(org_id=org.id, actions=None)
    kinds = {r.kind for r in all_audit_rows}
    assert "org.created" in kinds
    assert "membership.created" in kinds


# ---------------------------------------------------------------------------
# seed_github_install
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.service
async def test_seed_github_install_creates_expected_rows(db_session) -> None:
    """``seed_github_install`` inserts a GitHub install row + Claude Code settings row."""
    await seed_bootstrap_owner(
        email="gh-owner@example.com",
        github_id="gh-55",
        org_slug="gh-install-seed-org",
        display_name="GH Owner",
    )

    await seed_github_install(
        org_login="acme-test",
        target_org_slug="gh-install-seed-org",
    )

    org = await get_org_by_slug("gh-install-seed-org")
    assert org is not None

    # Verify GitHub install + Claude Code settings via audit rows emitted by seed.
    # seed_github_install calls install_coding_agent which emits coding_agent.installed.
    audit_rows = await list_for_org(org_id=org.id, actions=["coding_agent.installed"])
    assert len(audit_rows) >= 1

    # Verify Claude Code install via coding_agent.list_coding_agents.
    from app.core.coding_agent import list_coding_agents  # noqa: PLC0415

    agents = await list_coding_agents(db_session, org.id)
    claude_code_installs = [a for a in agents if a.plugin_id == "claude_code"]
    assert len(claude_code_installs) == 1


# ---------------------------------------------------------------------------
# seed_lesson
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.service
async def test_seed_lesson_returns_uuid_and_emits_audit(db_session) -> None:
    """``seed_lesson`` returns a lesson UUID and emits a ``lesson.created`` audit row."""
    from app.domain.lessons import get as get_lesson  # noqa: PLC0415
    from app.testing.e2e_setup.service import DEFAULT_ORG_ID  # noqa: PLC0415

    lesson_id = await seed_lesson(
        repo_external_id="acme/web",
        title="Always validate inputs",
        body="Never trust user-supplied data without validation.",
    )

    assert lesson_id is not None

    lesson = await get_lesson(lesson_id, org_id=DEFAULT_ORG_ID)
    assert lesson.title == "Always validate inputs"
    assert lesson.repo_external_id == "acme/web"

    # ``lessons.create`` emits a ``lesson.created`` audit row.
    audit_rows = await list_for_org(org_id=DEFAULT_ORG_ID, actions=["lesson.created"], limit=10)
    lesson_audits = [r for r in audit_rows if str(r.entity_id) == str(lesson_id)]
    assert len(lesson_audits) == 1


# ---------------------------------------------------------------------------
# seed_paused_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.service
async def test_seed_paused_run_writes_paused_run_and_open_pause(db_session) -> None:
    """``seed_paused_run`` writes a `paused` run with a completed skill stage
    (`boundary_outcome="paused"`) and a final artifact. Asserted via
    `list_runs_for_ticket` — the module's own public read model — rather than
    raw Row access, per the module-boundary rule on cross-module test reach."""
    ids = await seed_bootstrap_owner(
        email="owner@paused-run.test", github_id="gh-paused-1", org_slug="paused-run-org"
    )
    org = await get_org_by_slug("paused-run-org")
    assert org is not None

    result = await seed_paused_run(org_slug="paused-run-org", ticket_title="Seeded paused run")

    async with org_context(org.id, ActorKind.SYSTEM, actor_id=None):
        runs = await list_runs_for_ticket(UUID(result["ticket_id"]), session=db_session)
    assert len(runs) == 1
    run = runs[0]
    assert run.state == "paused"
    assert str(run.id) == result["run_id"]
    assert len(run.stages) == 1
    stage = run.stages[0]
    assert stage.status == "completed"
    assert stage.boundary_outcome == "paused"
    assert stage.confidence == "high"
    assert len(stage.artifact_ids) == 1

    from app.domain.artifacts import list_for_ticket  # noqa: PLC0415

    groups = await list_for_ticket(org.id, UUID(result["ticket_id"]), session=db_session)
    assert len(groups) == 1
    assert groups[0].versions[0].is_final is True

    assert ids["org_slug"] == "paused-run-org"


@pytest.mark.asyncio
@pytest.mark.service
async def test_seed_paused_run_approve_resumes_to_completion(db_session) -> None:
    """A seeded paused run's only stage resolves via `approve` straight to a
    `completed` terminal state — `workspace_id is None` skips cleanup."""
    owner_ids = await seed_bootstrap_owner(
        email="owner@paused-run-2.test", github_id="gh-paused-2", org_slug="paused-run-org-2"
    )
    org = await get_org_by_slug("paused-run-org-2")
    assert org is not None
    owner_user_id = UUID(owner_ids["user_id"])

    result = await seed_paused_run(org_slug="paused-run-org-2", ticket_title="Seeded paused run 2")

    # Empty escalation set — the owner authorizes via `is_pause_responder`'s
    # org-admin-union clause, same as the SPA's authenticated org owner would.
    async with org_context(org.id, ActorKind.USER, actor_id=owner_user_id):
        await resolve_pause(
            UUID(result["pause_id"]),
            resolution=PauseResolution(action="approve"),
            actor=Actor.user(user_id=owner_user_id),
            session=db_session,
        )
    await db_session.commit()

    from app.core.tasks import drain_once, get_broker, get_pending_task_names  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    for _ in range(10):
        pending = await get_pending_task_names(db_session)
        if not pending:
            break
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        if delivered == 0:
            break

    async with org_context(org.id, ActorKind.SYSTEM, actor_id=None):
        runs = await list_runs_for_ticket(UUID(result["ticket_id"]), session=db_session)
    assert len(runs) == 1
    assert runs[0].state == "completed"
