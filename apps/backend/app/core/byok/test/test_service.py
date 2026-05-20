"""Coverage for `core/byok` service: round-trip, clear, validate, audit."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core import byok
from app.core.audit_log import Actor
from app.core.audit_log.models import AuditEntryRow
from app.core.byok.models import ByokKeyRow
from app.domain.identity import repository as identity_repo
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.types import Role


@pytest.mark.asyncio
async def test_set_get_roundtrip(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="byok-rt")
    await orgs_repo.insert_membership(db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    await byok.set(org.id, "anthropic", "sk-secret", actor=actor, session=db_session)
    plaintext = await byok.get(org.id, "anthropic", session=db_session)
    assert plaintext == "sk-secret"


@pytest.mark.asyncio
async def test_get_returns_none_when_missing(db_session) -> None:
    org = await orgs_repo.insert_org(db_session, slug="byok-empty")
    assert await byok.get(org.id, "anthropic", session=db_session) is None


@pytest.mark.asyncio
async def test_set_overwrites_existing(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="byok-overwrite")
    await orgs_repo.insert_membership(db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    await byok.set(org.id, "anthropic", "first", actor=actor, session=db_session)
    await byok.set(org.id, "anthropic", "second", actor=actor, session=db_session)
    assert await byok.get(org.id, "anthropic", session=db_session) == "second"


@pytest.mark.asyncio
async def test_clear_removes_row(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="byok-clear")
    await orgs_repo.insert_membership(db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    await byok.set(org.id, "anthropic", "v", actor=actor, session=db_session)
    removed = await byok.clear(org.id, "anthropic", actor=actor, session=db_session)
    assert removed is True
    assert await byok.get(org.id, "anthropic", session=db_session) is None


@pytest.mark.asyncio
async def test_clear_returns_false_on_no_op(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="byok-noop")
    await orgs_repo.insert_membership(db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    assert await byok.clear(org.id, "anthropic", actor=actor, session=db_session) is False


@pytest.mark.asyncio
async def test_validate_invokes_callable_and_stamps_last_validated(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="byok-validate")
    await orgs_repo.insert_membership(db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    captured: list[str] = []

    async def _validator(plaintext: str) -> bool:
        captured.append(plaintext)
        return True

    await byok.set(org.id, "anthropic", "sk-validated", actor=actor, session=db_session)
    ok = await byok.validate(org.id, "anthropic", _validator, actor=actor, session=db_session)
    assert ok is True
    assert captured == ["sk-validated"]

    row = (
        await db_session.execute(
            select(ByokKeyRow).where(ByokKeyRow.org_id == org.id, ByokKeyRow.provider == "anthropic")
        )
    ).scalar_one()
    assert row.last_validated_at is not None


@pytest.mark.asyncio
async def test_validate_returns_false_when_validator_fails(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="byok-failval")
    await orgs_repo.insert_membership(db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    async def _bad(_: str) -> bool:
        return False

    await byok.set(org.id, "anthropic", "sk-bad", actor=actor, session=db_session)
    ok = await byok.validate(org.id, "anthropic", _bad, actor=actor, session=db_session)
    assert ok is False


@pytest.mark.asyncio
async def test_validate_returns_false_when_no_key_set(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="byok-noval")
    await orgs_repo.insert_membership(db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    async def _never(_: str) -> bool:
        raise AssertionError("validator must not be called")

    ok = await byok.validate(org.id, "anthropic", _never, actor=actor, session=db_session)
    assert ok is False


@pytest.mark.asyncio
async def test_set_emits_audit(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="byok-audit-set")
    await orgs_repo.insert_membership(db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    await byok.set(org.id, "anthropic", "sk-audit", actor=actor, session=db_session)
    rows = (
        (
            await db_session.execute(
                select(AuditEntryRow).where(AuditEntryRow.org_id == org.id, AuditEntryRow.kind == "byok.set")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload == {"provider": "anthropic"}


@pytest.mark.asyncio
async def test_clear_emits_audit_only_on_actual_removal(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="byok-audit-clear")
    await orgs_repo.insert_membership(db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    # No-op clear: no audit row.
    await byok.clear(org.id, "anthropic", actor=actor, session=db_session)
    rows = (
        (
            await db_session.execute(
                select(AuditEntryRow).where(
                    AuditEntryRow.org_id == org.id, AuditEntryRow.kind == "byok.cleared"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []

    # Real clear: one audit row.
    await byok.set(org.id, "anthropic", "v", actor=actor, session=db_session)
    await byok.clear(org.id, "anthropic", actor=actor, session=db_session)
    rows = (
        (
            await db_session.execute(
                select(AuditEntryRow).where(
                    AuditEntryRow.org_id == org.id, AuditEntryRow.kind == "byok.cleared"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_validate_audit_records_success_flag(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="byok-audit-val")
    await orgs_repo.insert_membership(db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    async def _ok(_: str) -> bool:
        return True

    async def _bad(_: str) -> bool:
        return False

    await byok.set(org.id, "anthropic", "k", actor=actor, session=db_session)
    await byok.validate(org.id, "anthropic", _ok, actor=actor, session=db_session)
    await byok.validate(org.id, "anthropic", _bad, actor=actor, session=db_session)

    rows = (
        (
            await db_session.execute(
                select(AuditEntryRow)
                .where(AuditEntryRow.org_id == org.id, AuditEntryRow.kind == "byok.validated")
                .order_by(AuditEntryRow.created_at)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert rows[0].payload == {"provider": "anthropic", "success": True}
    assert rows[1].payload == {"provider": "anthropic", "success": False}


@pytest.mark.asyncio
async def test_set_rejects_empty_string(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="byok-empty-input")
    await orgs_repo.insert_membership(db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)
    with pytest.raises(ValueError, match="non-empty"):
        await byok.set(org.id, "anthropic", "", actor=actor, session=db_session)
