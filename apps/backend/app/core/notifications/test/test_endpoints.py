"""Service-level coverage for /api/notifications/*.

Endpoints are session-cookie-only (no X-Yaaos-Org-Slug). Each test seeds two
users so we can prove the per-user scoping; mark-read is idempotent.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.notifications import web as _notifications_web  # noqa: F401
from app.core.notifications.models import NotificationRow
from app.core.notifications.service import create
from app.domain.orgs import repository as orgs_repo


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"notifications"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    alice = await identity_repo.insert_user(db_session, display_name="Alice")
    bob = await identity_repo.insert_user(db_session, display_name="Bob")
    org = await orgs_repo.insert_org(db_session, slug="notif-org", display_name="NotifOrg")
    # One notification for Alice, one for Bob — proves per-user scoping.
    n_alice = await create(
        user_id=alice.id,
        org_id=org.org_id,
        type="hitl_waiting",
        title="HITL prompt on PR #42",
        body="Reviewer needs a Builder decision before continuing.",
        session=db_session,
    )
    await create(
        user_id=bob.id,
        org_id=org.org_id,
        type="ticket_completed",
        title="Review done on PR #99",
        body="No high-severity findings.",
        session=db_session,
    )
    sess_alice = await session_lifecycle.create(db_session, user_id=alice.id, workspace_id=None)
    sess_bob = await session_lifecycle.create(db_session, user_id=bob.id, workspace_id=None)
    await db_session.commit()
    yield {
        "alice": alice,
        "bob": bob,
        "org": org,
        "n_alice": n_alice,
        "sess_alice": sess_alice,
        "sess_bob": sess_bob,
    }


@pytest.mark.asyncio
async def test_list_unauthenticated_returns_401() -> None:
    async with _client() as c:
        r = await c.get("/api/notifications")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_returns_only_callers_notifications(seeded) -> None:
    sess = seeded["sess_alice"]
    async with _client() as c:
        r = await c.get("/api/notifications", cookies={"yaaos_session": sess.raw_token})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["type"] == "hitl_waiting"
    assert body[0]["user_id"] == str(seeded["alice"].id)


@pytest.mark.asyncio
async def test_popover_returns_unread_count(seeded) -> None:
    sess = seeded["sess_alice"]
    async with _client() as c:
        r = await c.get("/api/notifications/popover", cookies={"yaaos_session": sess.raw_token})
    assert r.status_code == 200
    body = r.json()
    assert body["unread_count"] == 1
    assert len(body["items"]) == 1


@pytest.mark.asyncio
async def test_mark_one_read_is_idempotent(seeded, db_session) -> None:
    sess = seeded["sess_alice"]
    n_id = str(seeded["n_alice"].id)
    async with _client() as c:
        r1 = await c.post(
            f"/api/notifications/{n_id}/read",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-CSRF-Token": sess.csrf_token},
        )
        r2 = await c.post(
            f"/api/notifications/{n_id}/read",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-CSRF-Token": sess.csrf_token},
        )
    assert r1.status_code == 200
    assert r2.status_code == 200
    refreshed = await db_session.get(NotificationRow, seeded["n_alice"].id)
    assert refreshed is not None
    # read_at unchanged between r1 and r2 — the second call is a no-op.
    assert r1.json()["read_at"] == r2.json()["read_at"]
    assert refreshed.read_at is not None


@pytest.mark.asyncio
async def test_mark_all_read_marks_only_callers_rows(seeded) -> None:
    sess_alice = seeded["sess_alice"]
    async with _client() as c:
        r = await c.post(
            "/api/notifications/mark-read",
            cookies={"yaaos_session": sess_alice.raw_token, "yaaos_csrf": sess_alice.csrf_token},
            headers={"X-CSRF-Token": sess_alice.csrf_token},
            json={},
        )
    assert r.status_code == 200
    assert r.json()["marked"] == 1  # only Alice's one row

    # Bob's notification stays unread.
    sess_bob = seeded["sess_bob"]
    async with _client() as c:
        r = await c.get("/api/notifications/popover", cookies={"yaaos_session": sess_bob.raw_token})
    assert r.json()["unread_count"] == 1


@pytest.mark.asyncio
async def test_create_is_idempotent_by_user_type_subject(seeded, db_session) -> None:
    """Re-emitting the same (user, type, subject_type, subject_id) is a no-op."""
    from uuid import uuid4 as _uuid4  # noqa: PLC0415

    subject_id = _uuid4()
    first = await create(
        user_id=seeded["alice"].id,
        org_id=seeded["org"].org_id,
        type="ticket_completed",
        title="X",
        body="Y",
        subject_type="ticket",
        subject_id=subject_id,
        session=db_session,
    )
    second = await create(
        user_id=seeded["alice"].id,
        org_id=seeded["org"].org_id,
        type="ticket_completed",
        title="X (re-emit)",
        body="Y (re-emit)",
        subject_type="ticket",
        subject_id=subject_id,
        session=db_session,
    )
    await db_session.commit()
    assert first is not None
    assert second is None  # idempotent no-op
