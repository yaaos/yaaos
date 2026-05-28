"""Service test for the lessons-loop entry point:
`lessons.create(repo, title, body, source_pr_url)` inserts a lesson +
writes a `lesson.created` audit row.

The durable contract is the lesson row + audit, which is what this test
asserts.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.audit_log import Actor
from app.domain import lessons
from app.domain.orgs import repository as orgs_repo

pytestmark = pytest.mark.service


@pytest.mark.asyncio
async def test_teach_yaaos_creates_lesson_under_correct_repo_with_audit(db_session) -> None:
    org = await orgs_repo.insert_org(db_session, slug=f"svc-teach-{uuid4().hex[:8]}")
    await db_session.commit()

    title = "Cite the CWE family"
    body = "When flagging an input-validation issue, name the CWE family."

    lesson = await lessons.create(
        "owner/repo",
        title,
        body,
        source_pr_url="https://example.test/owner/repo/pull/21",
        actor=Actor.system(),
        org_id=org.id,
        plugin_id="github",
    )

    # Lesson row exists with the right repo binding.
    row = (
        await db_session.execute(
            text(
                "SELECT title, body, source_pr_url, repo_external_id, plugin_id, org_id"
                " FROM lessons WHERE id=:id"
            ),
            {"id": lesson.id},
        )
    ).one()
    assert row[0] == title
    assert row[1] == body
    assert row[2] == "https://example.test/owner/repo/pull/21"
    assert row[3] == "owner/repo"
    assert row[4] == "github"
    assert row[5] == org.id

    # Audit row for the creation.
    audit = (
        await db_session.execute(
            text("SELECT kind FROM audit_entries WHERE entity_kind='lesson' AND entity_id=:id"),
            {"id": lesson.id},
        )
    ).scalar_one()
    assert audit == "lesson.created"

    # Cross-check: list_for_repo finds it.
    listed = await lessons.list_for_repo("owner/repo", org_id=org.id, plugin_id="github")
    assert any(l.id == lesson.id for l in listed)
