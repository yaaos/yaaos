"""Service-level coverage for `list_lessons` filters.

Asserts q (substring), repo multi-select, sort, created_by, and date
range. Each test runs against real Postgres via `db_session`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.identity import repository as identity_repo
from app.domain.lessons.service import LessonFilter, list_lessons
from app.domain.orgs import repository as orgs_repo


@pytest_asyncio.fixture
async def seeded(db_session):
    alice = await identity_repo.insert_user(db_session, display_name="A")
    bob = await identity_repo.insert_user(db_session, display_name="B")
    org = await orgs_repo.insert_org(db_session, slug="lf-org")

    now = datetime.now(UTC)
    rows = [
        # (title, body, repo, created_by, created_at_offset_days)
        ("retries", "tighten the retry policy", "x/api", alice.id, 0),
        ("timeouts", "bump default timeout", "x/api", bob.id, -1),
        ("noise", "ignore generated files", "x/web", alice.id, -5),
        ("frontend", "watch out for hydration", "x/web", bob.id, -30),
    ]
    for title, body, repo, created_by, days in rows:
        await db_session.execute(
            text(
                "INSERT INTO lessons"
                " (id, org_id, plugin_id, repo_external_id, title, body, created_by, created_at,"
                "  updated_at)"
                " VALUES (:id, :org_id, 'github', :repo, :title, :body, :cb, :ts, :ts)"
            ),
            {
                "id": uuid4(),
                "org_id": org.id,
                "repo": repo,
                "title": title,
                "body": body,
                "cb": created_by,
                "ts": now + timedelta(days=days),
            },
        )
    await db_session.commit()
    yield {"alice": alice, "bob": bob, "org": org, "now": now}


@pytest.mark.service
@pytest.mark.asyncio
async def test_q_matches_title_or_body(seeded) -> None:
    rows = await list_lessons(LessonFilter(q="retry"), org_id=seeded["org"].id)
    assert {r.title for r in rows} == {"retries"}  # body match


@pytest.mark.service
@pytest.mark.asyncio
async def test_repo_multi_filter(seeded) -> None:
    rows = await list_lessons(LessonFilter(repo_external_ids=["x/api"]), org_id=seeded["org"].id)
    assert {r.title for r in rows} == {"retries", "timeouts"}


@pytest.mark.service
@pytest.mark.asyncio
async def test_created_by_filter(seeded) -> None:
    rows = await list_lessons(LessonFilter(created_by=seeded["alice"].id), org_id=seeded["org"].id)
    assert {r.title for r in rows} == {"retries", "noise"}


@pytest.mark.service
@pytest.mark.asyncio
async def test_sort_created_asc(seeded) -> None:
    rows = await list_lessons(LessonFilter(sort="created_asc"), org_id=seeded["org"].id)
    assert [r.title for r in rows] == ["frontend", "noise", "timeouts", "retries"]


@pytest.mark.service
@pytest.mark.asyncio
async def test_date_range(seeded) -> None:
    # Last 7 days: today + yesterday + 5 days ago.
    seven_days_ago = seeded["now"] - timedelta(days=7)
    rows = await list_lessons(LessonFilter(created_after=seven_days_ago), org_id=seeded["org"].id)
    assert {r.title for r in rows} == {"retries", "timeouts", "noise"}
