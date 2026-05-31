"""Bearer ledger: issue, verify, revoke."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.core.agent_gateway import bearers
from app.core.agent_gateway.models import BearerTokenRow, WorkspaceAgentRow
from app.domain.orgs import repository as orgs_repo


async def _fixture_org_and_agent(db_session) -> tuple:
    """Insert a configured org + an agent pod row. Returns (org_id, agent_id)."""
    org = await orgs_repo.insert_org(db_session, slug=f"bearer-{uuid4().hex[:6]}")
    org.registered_iam_arn = f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}"
    org.aws_region = "us-east-1"
    agent = WorkspaceAgentRow(
        id=uuid4(),
        org_id=org.org_id,
        instance_id=f"test-task-{uuid4().hex[:8]}",
        iam_arn=org.registered_iam_arn,
        version="0.0.1",
        state="reachable",
    )
    db_session.add(agent)
    await db_session.commit()
    return org.org_id, agent.id


async def test_issue_returns_plaintext_and_persists_hash(db_session) -> None:
    org_id, agent_id = await _fixture_org_and_agent(db_session)

    plaintext, record = await bearers.issue(
        agent_id=agent_id, org_id=org_id, session=db_session, source_ip="192.0.2.1"
    )
    await db_session.commit()

    assert plaintext  # 43-ish urlsafe base64 chars
    assert len(plaintext) >= 40
    assert record.agent_id == agent_id
    assert record.org_id == org_id
    assert record.source_ip == "192.0.2.1"

    # The persisted row contains the hash, not the plaintext.
    row = (
        await db_session.execute(select(BearerTokenRow).where(BearerTokenRow.id == record.id))
    ).scalar_one()
    assert row.token_hash != plaintext.encode("utf-8")
    assert len(row.token_hash) == 32  # sha256 digest
    assert row.revoked_at is None


async def test_verify_happy_path(db_session) -> None:
    org_id, agent_id = await _fixture_org_and_agent(db_session)
    plaintext, record = await bearers.issue(agent_id=agent_id, org_id=org_id, session=db_session)
    await db_session.commit()

    ctx = await bearers.verify(plaintext)
    assert ctx is not None
    assert ctx.bearer_id == record.id
    assert ctx.agent_id == agent_id
    assert ctx.org_id == org_id


async def test_verify_rejects_unknown_token() -> None:
    assert await bearers.verify("not-a-real-token") is None
    assert await bearers.verify("") is None


async def test_verify_rejects_expired(db_session) -> None:
    org_id, agent_id = await _fixture_org_and_agent(db_session)
    plaintext, record = await bearers.issue(
        agent_id=agent_id,
        org_id=org_id,
        session=db_session,
        ttl_seconds=1,
    )
    # Force expiry by backdating the row.
    row = (
        await db_session.execute(select(BearerTokenRow).where(BearerTokenRow.id == record.id))
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    assert await bearers.verify(plaintext) is None


async def test_verify_rejects_revoked(db_session) -> None:
    org_id, agent_id = await _fixture_org_and_agent(db_session)
    plaintext, record = await bearers.issue(agent_id=agent_id, org_id=org_id, session=db_session)
    await bearers.revoke(record.id, "manual_rotate", session=db_session)
    await db_session.commit()

    assert await bearers.verify(plaintext) is None


async def test_revoke_all_for_agent_revokes_only_that_agent(db_session) -> None:
    org_id, agent_id_a = await _fixture_org_and_agent(db_session)
    # Second agent in the same org
    other_agent = WorkspaceAgentRow(
        id=uuid4(),
        org_id=org_id,
        instance_id=f"test-task-other-{uuid4().hex[:8]}",
        iam_arn="arn:aws:iam::123456789012:role/other",
        version="0.0.1",
        state="reachable",
    )
    db_session.add(other_agent)
    await db_session.commit()

    plain_a, _ = await bearers.issue(agent_id=agent_id_a, org_id=org_id, session=db_session)
    plain_b, _ = await bearers.issue(agent_id=other_agent.id, org_id=org_id, session=db_session)
    await db_session.commit()

    count = await bearers.revoke_all_for_agent(agent_id_a, "agent_loss", session=db_session)
    await db_session.commit()
    assert count == 1

    assert await bearers.verify(plain_a) is None
    assert await bearers.verify(plain_b) is not None


async def test_revoke_all_for_org_revokes_every_active(db_session) -> None:
    org_id, agent_id = await _fixture_org_and_agent(db_session)
    plain_1, _ = await bearers.issue(agent_id=agent_id, org_id=org_id, session=db_session)
    plain_2, _ = await bearers.issue(agent_id=agent_id, org_id=org_id, session=db_session)
    await db_session.commit()

    count = await bearers.revoke_all_for_org(org_id, "disconnect", session=db_session)
    await db_session.commit()
    assert count == 2

    assert await bearers.verify(plain_1) is None
    assert await bearers.verify(plain_2) is None


async def test_revoke_is_idempotent(db_session) -> None:
    org_id, agent_id = await _fixture_org_and_agent(db_session)
    _, record = await bearers.issue(agent_id=agent_id, org_id=org_id, session=db_session)
    await bearers.revoke(record.id, "manual_rotate", session=db_session)
    await db_session.commit()

    # Second revoke should not overwrite the original reason or timestamp.
    row_before = (
        await db_session.execute(select(BearerTokenRow).where(BearerTokenRow.id == record.id))
    ).scalar_one()
    first_ts = row_before.revoked_at

    await bearers.revoke(record.id, "agent_loss", session=db_session)
    await db_session.commit()

    row_after = (
        await db_session.execute(select(BearerTokenRow).where(BearerTokenRow.id == record.id))
    ).scalar_one()
    assert row_after.revoked_at == first_ts
    assert row_after.revoked_reason == "manual_rotate"


async def test_list_for_org_returns_recent_first(db_session) -> None:
    org_id, agent_id = await _fixture_org_and_agent(db_session)
    _, first = await bearers.issue(agent_id=agent_id, org_id=org_id, session=db_session)
    _, second = await bearers.issue(agent_id=agent_id, org_id=org_id, session=db_session)
    await db_session.commit()

    records = await bearers.list_for_org(org_id)
    assert [r.id for r in records[:2]] == [second.id, first.id]


async def test_verify_bumps_last_seen(db_session) -> None:
    org_id, agent_id = await _fixture_org_and_agent(db_session)
    plaintext, record = await bearers.issue(agent_id=agent_id, org_id=org_id, session=db_session)
    await db_session.commit()

    before = (
        await db_session.execute(select(BearerTokenRow).where(BearerTokenRow.id == record.id))
    ).scalar_one()
    assert before.last_seen_at is None

    assert await bearers.verify(plaintext) is not None

    await db_session.refresh(before)
    assert before.last_seen_at is not None


async def test_issue_records_issued_iam_arn(db_session) -> None:
    """Bearer row persists the canonical IAM ARN for audit."""
    org_id, agent_id = await _fixture_org_and_agent(db_session)
    arn = "arn:aws:iam::123456789012:role/yaaos-agent"

    _plaintext, record = await bearers.issue(
        agent_id=agent_id,
        org_id=org_id,
        session=db_session,
        issued_iam_arn=arn,
    )
    await db_session.commit()

    row = (
        await db_session.execute(select(BearerTokenRow).where(BearerTokenRow.id == record.id))
    ).scalar_one()
    assert row.issued_iam_arn == arn
    assert record.issued_iam_arn == arn


async def test_issue_default_ttl_is_one_hour(db_session) -> None:
    """Default TTL is 1 hour (not 24h)."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    org_id, agent_id = await _fixture_org_and_agent(db_session)
    before = datetime.now(UTC)
    _plaintext, record = await bearers.issue(agent_id=agent_id, org_id=org_id, session=db_session)
    after = datetime.now(UTC)
    await db_session.commit()

    # expires_at should be roughly 1 hour from issuance.
    assert record.expires_at >= before + timedelta(minutes=59)
    assert record.expires_at <= after + timedelta(hours=1, minutes=1)
