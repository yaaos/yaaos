"""Service-level coverage for the finding ack/push-back endpoints.

- POST /api/reviewer/findings/{finding_id}/ack
- POST /api/reviewer/findings/{finding_id}/push-back
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text

import app.web  # noqa: F401  — registers the reviewer router
from app.core.auth import AuthMiddleware
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.domain.orgs import Role
from app.domain.orgs import repository as orgs_repo
from app.domain.reviewer.aggregate import RawFinding
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.types import CodeAnchor, FindingFingerprint, FindingState


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"reviewer"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


def _raw_finding() -> RawFinding:
    return RawFinding(
        fingerprint=FindingFingerprint(
            file_path="src/foo.py",
            rule_id="x/null-deref",
            anchor_content_hash="anc",
            body_gist_hash="gist",
        ),
        rule_id="x/null-deref",
        title="x could be None",
        body="caller may pass None",
        rationale="raises NoneType",
        concrete_failure_scenario="None propagates",
        confidence=90,
        severity="major",
        anchor=CodeAnchor(
            file_path="src/foo.py",
            line_start=10,
            line_end=10,
            surrounding_content_hash="surr",
            commit_sha="abc",
        ),
        source_agent="test",
    )


@pytest_asyncio.fixture
async def seeded(db_session):
    """Seed: org + Builder user + session + ticket + PR + Review + one finding."""
    org = await orgs_repo.insert_org(db_session, slug="ack-org")
    user = await identity_repo.insert_user(db_session, display_name="Bob")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.id, role=Role.BUILDER, handle="b"
    )
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)

    ticket_id, pr_id, review_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status,"
            " plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', 'ack/repo#1', 't', 'in_review',"
            " 'github', 'ack/repo')"
        ),
        {"id": ticket_id, "org_id": org.id},
    )
    await db_session.execute(
        text(
            "INSERT INTO pull_requests (id, org_id, ticket_id, plugin_id, external_id,"
            " repo_external_id, number, title, body, author_login, author_type,"
            " base_branch, head_branch, base_sha, head_sha, is_draft, is_fork, state, html_url)"
            " VALUES (:id, :org_id, :tid, 'github', 'ack/repo#1', 'ack/repo', 1, 't', '',"
            " 'dev', 'user', 'main', 'feat', 'b', 'h', false, false, 'open', 'https://x')"
        ),
        {"id": pr_id, "org_id": org.id, "tid": ticket_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status, trigger_reason,"
            " scope_kind, destination)"
            " VALUES (:id, :org_id, :pr_id, 1, 'queued', 'pr_ready', 'full', 'vcs')"
        ),
        {"id": review_id, "org_id": org.id, "pr_id": pr_id},
    )

    # Insert a finding row directly — the aggregate dedup / fingerprint logic
    # has its own coverage; here we just need a target for the ack endpoints.
    finding_id = uuid.uuid4()
    anchor_json = (
        '{"file_path":"src/foo.py","line_start":1,"line_end":1,'
        '"surrounding_content_hash":"s","commit_sha":"abc"}'
    )
    await db_session.execute(
        text(
            "INSERT INTO findings (id, org_id, pr_id, fingerprint_hash, rule_id, title, body,"
            " rationale, concrete_failure_scenario, confidence, severity, state, current_anchor,"
            " source_agent, first_seen_review_id, last_observed_review_id)"
            " VALUES (:id, :org_id, :pr_id, 'fp', 'x/null', 't', 'b', 'r', 'f', 90, 'major', 'open',"
            " CAST(:anchor AS jsonb), 'test', :rid, :rid)"
        ),
        {
            "id": finding_id,
            "org_id": org.id,
            "pr_id": pr_id,
            "rid": review_id,
            "anchor": anchor_json,
        },
    )
    await db_session.commit()

    yield {"org": org, "sess": sess, "finding_id": finding_id, "pr_id": pr_id}


def _auth(sess, slug: str):  # type: ignore[no-untyped-def]
    return {
        "cookies": {"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
        "headers": {"X-Org-Slug": slug, "X-CSRF-Token": sess.csrf_token},
    }


@pytest.mark.asyncio
async def test_ack_transitions_finding_to_acknowledged(seeded, db_session) -> None:  # type: ignore[no-untyped-def]
    finding_id = str(seeded["finding_id"])
    async with _client() as c:
        r = await c.post(
            f"/api/reviewer/findings/{finding_id}/ack",
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "acked"

    repo = SqlAlchemyAggregateRepository(db_session)
    agg = await repo.load(pr_id=seeded["pr_id"], org_id=seeded["org"].id)
    assert agg.findings[0].state == FindingState.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_push_back_requires_reason(seeded) -> None:  # type: ignore[no-untyped-def]
    finding_id = str(seeded["finding_id"])
    async with _client() as c:
        r = await c.post(
            f"/api/reviewer/findings/{finding_id}/push-back",
            json={"reason": "too short"},
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_push_back_records_wontfix(seeded, db_session) -> None:  # type: ignore[no-untyped-def]
    finding_id = str(seeded["finding_id"])
    reason = "intentional null on construction; documented elsewhere"
    async with _client() as c:
        r = await c.post(
            f"/api/reviewer/findings/{finding_id}/push-back",
            json={"reason": reason},
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "pushed_back"

    repo = SqlAlchemyAggregateRepository(db_session)
    agg = await repo.load(pr_id=seeded["pr_id"], org_id=seeded["org"].id)
    assert agg.findings[0].state == FindingState.ACKNOWLEDGED  # internal vocab
    acks = agg._state.acks  # repository persists the AcknowledgmentDecision row(s)
    assert acks[0].kind == "wontfix"
    assert acks[0].rationale == reason


@pytest.mark.asyncio
async def test_ack_404_on_missing_finding(seeded) -> None:  # type: ignore[no-untyped-def]
    async with _client() as c:
        r = await c.post(
            f"/api/reviewer/findings/{uuid.uuid4()}/ack",
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 404
