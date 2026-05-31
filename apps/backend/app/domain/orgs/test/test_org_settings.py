"""Coverage for PATCH /api/orgs (session_timeout_override) + the idle-timeout
check the require() dep performs based on the org's override."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware, Role
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.sessions import web as _auth_web  # noqa: F401
from app.core.tenancy import get_org_full, update_org_fields
from app.domain.orgs import org_settings_web as _org_settings_web  # noqa: F401
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs import web as _orgs_web  # noqa: F401
from app.testing.seed import set_session_last_seen as _set_session_last_seen_for_tests


def _patch_app() -> FastAPI:

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"orgs"})
    return app


def _patch_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_patch_app()), base_url="http://test")


def _idle_probe_app() -> FastAPI:
    """An app with a single MEMBERS_READ-gated endpoint so we can prove the
    idle-timeout check inside `require()` rejects an old session."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    # Reuse the memberships router so we have an org-scoped GET to hit.
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"memberships"})
    return app


def _idle_probe_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_idle_probe_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    owner = await identity_repo.insert_user(db_session, display_name="O")
    admin = await identity_repo.insert_user(db_session, display_name="A")
    member = await identity_repo.insert_user(db_session, display_name="M")
    org = await orgs_repo.insert_org(db_session, slug="ts-org")
    await orgs_repo.insert_membership(
        db_session, user_id=owner.id, org_id=org.org_id, role=Role.OWNER, handle="own"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=admin.id, org_id=org.org_id, role=Role.ADMIN, handle="adm"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member.id, org_id=org.org_id, role=Role.BUILDER, handle="mem"
    )
    admin_sess = await session_lifecycle.create(db_session, user_id=admin.id, workspace_id=None)
    member_sess = await session_lifecycle.create(db_session, user_id=member.id, workspace_id=None)
    await db_session.commit()
    yield {
        "org": org,
        "admin_sess": admin_sess,
        "member_sess": member_sess,
    }


# ── PATCH /api/orgs ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_org_unauthenticated_401(seeded) -> None:
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"session_timeout_override": 30},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": "x"},
            cookies={"yaaos_csrf": "x"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_patch_org_member_forbidden(seeded) -> None:
    sess = seeded["member_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"session_timeout_override": 30},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_get_org_settings_returns_current_values(seeded, db_session) -> None:
    """GET /api/orgs returns the current org's top-level settings so the SPA
    can show what's set before the user edits."""
    # Seed some non-default values to assert they round-trip.
    await update_org_fields(
        db_session,
        seeded["org"].org_id,
        session_timeout_override=42,
        registered_iam_arn="arn:aws:iam::123456789012:role/yaaos-agent",
        aws_region="us-east-1",
    )
    await db_session.commit()

    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.get(
            "/api/orgs",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == seeded["org"].slug
    assert body["session_timeout_override"] == 42
    assert body["registered_iam_arn"] == "arn:aws:iam::123456789012:role/yaaos-agent"
    assert body["aws_region"] == "us-east-1"


@pytest.mark.asyncio
async def test_get_org_settings_defaults_when_unset(seeded) -> None:
    """A freshly seeded org has no registered_iam_arn / session_timeout_override
    set — GET returns nulls."""
    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.get(
            "/api/orgs",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_timeout_override"] is None
    assert body["registered_iam_arn"] is None


@pytest.mark.asyncio
async def test_patch_org_admin_can_set_override(seeded, db_session) -> None:
    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"session_timeout_override": 30},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == seeded["org"].slug
    assert body["session_timeout_override"] == 30

    full = await get_org_full(db_session, seeded["org"].org_id)
    assert full is not None
    assert full.session_timeout_override == 30


@pytest.mark.asyncio
async def test_patch_org_admin_can_clear_override(seeded, db_session) -> None:
    # Pre-set, then clear.
    await update_org_fields(db_session, seeded["org"].org_id, session_timeout_override=30)
    await db_session.commit()

    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"session_timeout_override": None},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json()["session_timeout_override"] is None


@pytest.mark.asyncio
async def test_patch_org_rejects_non_positive(seeded) -> None:
    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"session_timeout_override": 0},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_org_admin_can_set_arn_and_region(seeded, db_session) -> None:
    """The SPA card PATCHes IAM ARN + region. The happy path returns 200
    with the new values and a subsequent GET sees them — the SPA's save →
    re-hydrate round-trip."""
    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={
                "registered_iam_arn": "arn:aws:iam::123456789012:role/yaaos-agent",
                "aws_region": "us-east-1",
            },
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registered_iam_arn"] == "arn:aws:iam::123456789012:role/yaaos-agent"
    assert body["aws_region"] == "us-east-1"

    # The SPA re-fetches via GET after a successful save; that call sees the
    # same values.
    async with _patch_client() as c:
        r2 = await c.get(
            "/api/orgs",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r2.status_code == 200
    assert r2.json()["registered_iam_arn"] == "arn:aws:iam::123456789012:role/yaaos-agent"


@pytest.mark.asyncio
async def test_patch_org_can_clear_arn(seeded, db_session) -> None:
    """Saving null ARN + region clears the workspace config."""
    # Pre-set ARN.
    await update_org_fields(
        db_session,
        seeded["org"].org_id,
        registered_iam_arn="arn:aws:iam::123456789012:role/yaaos-agent",
        aws_region="us-east-1",
    )
    await db_session.commit()

    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"registered_iam_arn": None, "aws_region": None},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registered_iam_arn"] is None


@pytest.mark.parametrize(
    "bad_arn",
    [
        "not-an-arn",
        # Wrong service.
        "arn:aws:sts::123456789012:role/yaaos-agent",
        # Wrong partition (only `aws` accepted — gov/cn deferred).
        "arn:aws-us-gov:iam::123456789012:role/yaaos-agent",
        # Account isn't 12 digits.
        "arn:aws:iam::12345:role/yaaos-agent",
        # Role path — would collide on canonicalization with the no-path form
        # (`assumed-role` ARNs strip the path), so two orgs could canonicalize
        # to the same string. Forbid at registration.
        "arn:aws:iam::123456789012:role/team/yaaos-agent",
        # Wrong resource type.
        "arn:aws:iam::123456789012:user/jack",
        # Trailing garbage — regex must full-match.
        "arn:aws:iam::123456789012:role/yaaos-agent extra",
    ],
)
@pytest.mark.asyncio
async def test_patch_org_rejects_malformed_arn(seeded, bad_arn: str) -> None:
    """Registration regex full-matches `arn:aws:iam::<12-digit>:role/<name>`
    with no path slashes — the only shape that round-trips through STS
    canonicalization. Anything else gets 422 so a misconfigured customer
    discovers the problem at save time, not at first identity exchange."""
    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={
                "registered_iam_arn": bad_arn,
                "aws_region": "us-east-1",
            },
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "invalid_registered_iam_arn"


@pytest.mark.asyncio
async def test_patch_org_lowercases_arn(seeded) -> None:
    """ARN comparison is case-insensitive (IAM names are unique-case-insensitive
    in AWS), implemented by lowercasing at write time + lowercasing in
    `canonicalize_arn`. A customer who types `MyRole` is stored as `myrole`
    and matches STS's response regardless of returned case."""
    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={
                "registered_iam_arn": "arn:aws:iam::123456789012:role/Yaaos-Agent",
                "aws_region": "us-east-1",
            },
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json()["registered_iam_arn"] == "arn:aws:iam::123456789012:role/yaaos-agent"


@pytest.mark.asyncio
async def test_patch_org_ignores_unrelated_keys(seeded, db_session) -> None:
    """Keys we don't recognise are silently ignored — the body schema is
    open-ended so future settings can be added without breaking older
    clients."""
    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"future_field": "ignored", "session_timeout_override": 45},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json()["session_timeout_override"] == 45


# ── Idle timeout (per-org override) ──────────────────────────────────────────


async def _backdate_session_last_seen(db_session, *, token_hash: str, minutes_ago: int) -> None:
    """Test helper: rewrite a session row's `last_seen_at` to simulate idleness."""
    await _set_session_last_seen_for_tests(
        db_session,
        token_hash=token_hash,
        last_seen_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_idle_session_rejected_when_override_set(seeded, db_session) -> None:
    """Admin pins the override to 10 minutes; a session last seen 30 minutes
    ago is rejected by the require() dep with 401 session_idle_expired."""
    await update_org_fields(db_session, seeded["org"].org_id, session_timeout_override=10)
    sess = seeded["admin_sess"]
    await _backdate_session_last_seen(
        db_session, token_hash=identity_repo.hash_token(sess.raw_token), minutes_ago=30
    )

    async with _idle_probe_client() as c:
        r = await c.get(
            "/api/memberships",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 401, r.text
    assert r.json()["detail"]["error"] == "session_idle_expired"


@pytest.mark.asyncio
async def test_idle_session_within_override_passes(seeded, db_session) -> None:
    """Within the override window: passes."""
    await update_org_fields(db_session, seeded["org"].org_id, session_timeout_override=60)
    sess = seeded["admin_sess"]
    await _backdate_session_last_seen(
        db_session, token_hash=identity_repo.hash_token(sess.raw_token), minutes_ago=30
    )

    async with _idle_probe_client() as c:
        r = await c.get(
            "/api/memberships",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_idle_default_used_when_override_null(seeded, db_session) -> None:
    """No override → global SESSION_IDLE_TIMEOUT (12h) governs. A 30-minute
    idle session is still fresh under the default."""
    sess = seeded["admin_sess"]
    await _backdate_session_last_seen(
        db_session, token_hash=identity_repo.hash_token(sess.raw_token), minutes_ago=30
    )
    async with _idle_probe_client() as c:
        r = await c.get(
            "/api/memberships",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
