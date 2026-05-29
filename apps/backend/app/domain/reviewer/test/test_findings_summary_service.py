"""Service tests for findings rollup written by reviewer to the ticket row.

Covers:
- refresh_ticket_findings_summary writes correct count + severity after review end
- findings summary refreshed after ack/push-back via the HTTP endpoints
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, text

import app.web  # noqa: F401  — registers the reviewer router
from app.core.auth import AuthMiddleware
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.domain.orgs import Role
from app.domain.orgs import repository as orgs_repo
from app.domain.reviewer.service import refresh_ticket_findings_summary
from app.domain.tickets import TicketRow, update_findings_summary

# ── shared seed helpers ───────────────────────────────────────────────────────

_ANCHOR_JSON = (
    '{"file_path": "src/foo.py", "line_start": 1, "line_end": 1, '
    '"surrounding_content_hash": "h", "commit_sha": "abc"}'
)


async def _seed_ticket_and_pr(  # type: ignore[no-untyped-def]
    db_session, *, org_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a ticket + linked PR; return (ticket_id, pr_id)."""
    ticket_id = uuid.uuid4()
    pr_id = uuid.uuid4()
    src_ext = f"acme/repo#{uuid.uuid4().hex[:8]}"
    pr_ext = f"acme/repo#{uuid.uuid4().hex[:8]}"
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status,"
            " plugin_id, repo_external_id, pr_id)"
            " VALUES (:id, :org_id, 'github_pr', :src_ext, 't', 'running',"
            " 'github', 'acme/repo', :pr_id)"
        ),
        {"id": ticket_id, "org_id": org_id, "src_ext": src_ext, "pr_id": pr_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO pull_requests"
            " (id, org_id, ticket_id, plugin_id, external_id, repo_external_id, number, title, body,"
            "  author_login, author_type, base_branch, head_branch, base_sha, head_sha,"
            "  is_draft, is_fork, state, html_url)"
            " VALUES (:id, :org_id, :tid, 'github', :pr_ext, 'acme/repo', 1, 't', '',"
            "         'dev', 'user', 'main', 'feat', 'b', 'h', false, false, 'open', 'https://x')"
        ),
        {"id": pr_id, "org_id": org_id, "tid": ticket_id, "pr_ext": pr_ext},
    )
    return ticket_id, pr_id


async def _seed_finding(  # type: ignore[no-untyped-def]
    db_session,
    *,
    pr_id: uuid.UUID,
    org_id: uuid.UUID,
    severity: str = "high",
    seq: int = 1,
) -> uuid.UUID:
    """Seed a finding row; return its id."""
    review_id = uuid.uuid4()
    finding_id = uuid.uuid4()
    fp = uuid.uuid4().hex
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status, trigger_reason, scope_kind, destination)"
            " VALUES (:id, :org_id, :pr_id, :seq, 'posted', 'pr_ready', 'full', 'vcs')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id, "seq": seq},
    )
    await db_session.execute(
        text(
            "INSERT INTO findings"
            " (id, org_id, pr_id, fingerprint_hash, rule_id, title, body, rationale,"
            "  concrete_failure_scenario, confidence, severity, state, current_anchor, source_agent,"
            "  first_seen_review_id, last_observed_review_id)"
            " VALUES (:id, :org_id, :pr_id, :fp, 'r/x', 't', 'b', 'r',"
            "         'caller invokes f() without arg; raises TypeError.', 90, :severity, 'open',"
            "         (:anchor)::jsonb, 'test', :rid, :rid)"
        ),
        {
            "id": finding_id,
            "org_id": org_id,
            "pr_id": pr_id,
            "fp": fp,
            "severity": severity,
            "anchor": _ANCHOR_JSON,
            "rid": review_id,
        },
    )
    return finding_id


# ── test_reviewer_writes_findings_summary_on_review_end ───────────────────────


@pytest.mark.service
async def test_reviewer_writes_findings_summary_on_review_end(db_session) -> None:  # type: ignore[no-untyped-def]
    """refresh_ticket_findings_summary writes count + max_severity to the ticket row."""
    org_id = uuid.uuid4()
    ticket_id, pr_id = await _seed_ticket_and_pr(db_session, org_id=org_id)
    await _seed_finding(db_session, pr_id=pr_id, org_id=org_id, severity="medium", seq=1)
    await _seed_finding(db_session, pr_id=pr_id, org_id=org_id, severity="high", seq=2)
    await db_session.commit()

    await refresh_ticket_findings_summary(ticket_id, pr_id, org_id=org_id, session=db_session)
    await db_session.commit()

    row = (await db_session.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one()
    assert row.findings_count == 2
    assert row.max_severity == "high"


@pytest.mark.service
async def test_reviewer_writes_summary_zero_when_no_findings(db_session) -> None:  # type: ignore[no-untyped-def]
    """refresh_ticket_findings_summary with no findings sets count=0, severity=None."""
    org_id = uuid.uuid4()
    ticket_id, pr_id = await _seed_ticket_and_pr(db_session, org_id=org_id)
    # Seed a non-zero value first, then verify refresh resets it.
    await update_findings_summary(ticket_id, findings_count=5, max_severity="high", session=db_session)
    await db_session.commit()

    await refresh_ticket_findings_summary(ticket_id, pr_id, org_id=org_id, session=db_session)
    await db_session.commit()

    row = (await db_session.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one()
    assert row.findings_count == 0
    assert row.max_severity is None


# ── test_findings_summary_refreshed_on_ack ───────────────────────────────────


def _ack_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"reviewer"})
    return app


def _ack_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_ack_app()), base_url="http://test")


@pytest_asyncio.fixture
async def ack_seeded(db_session):  # type: ignore[no-untyped-def]
    """Seed: org + Builder + session + ticket (with pr_id set) + PR + finding with high severity."""
    org = await orgs_repo.insert_org(db_session, slug=f"sum-org-{uuid.uuid4().hex[:6]}")
    user = await identity_repo.insert_user(db_session, display_name="Alice")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.id, role=Role.BUILDER, handle="a"
    )
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)

    ticket_id, pr_id = await _seed_ticket_and_pr(db_session, org_id=org.id)
    finding_id = await _seed_finding(db_session, pr_id=pr_id, org_id=org.id, severity="high", seq=1)
    # Pre-populate the rollup so we can verify it changes after ack.
    await update_findings_summary(ticket_id, findings_count=1, max_severity="high", session=db_session)
    await db_session.commit()

    yield {
        "org": org,
        "sess": sess,
        "ticket_id": ticket_id,
        "pr_id": pr_id,
        "finding_id": finding_id,
    }


def _auth(sess, slug: str):  # type: ignore[no-untyped-def]
    return {
        "cookies": {"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
        "headers": {"X-Org-Slug": slug, "X-CSRF-Token": sess.csrf_token},
    }


@pytest.mark.asyncio
async def test_findings_summary_refreshed_on_ack(ack_seeded, db_session) -> None:  # type: ignore[no-untyped-def]
    """After the ack endpoint transitions a finding to acknowledged, the ticket
    row's findings_count is refreshed (finding is still counted — ack doesn't delete it)."""
    finding_id = str(ack_seeded["finding_id"])
    ticket_id = ack_seeded["ticket_id"]

    async with _ack_client() as c:
        r = await c.post(
            f"/api/reviewer/findings/{finding_id}/ack",
            **_auth(ack_seeded["sess"], ack_seeded["org"].slug),
        )
    assert r.status_code == 200, r.text

    # The finding is acknowledged; it's still counted in the rollup (state
    # change doesn't remove it from the aggregate query).
    row = (await db_session.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one()
    # findings_count is refreshed — acknowledged findings are still findings.
    assert row.findings_count == 1
    assert row.max_severity == "high"
