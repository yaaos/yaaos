"""Service test: version sequencing, `mark_final` gating of `latest_final`,
and the read-only HTTP surface.

`pipeline_runs`/`stage_executions` rows (owned by the sibling `domain/pipelines`
module) are seeded via raw SQL — the sanctioned test-file mechanism for
cross-module state without a `*Row` cross-module import (patterns.md
§ Module boundaries in tests; `bin/check_table_access` exempts test files
from the raw-SQL ownership scan for exactly this reason). Mirrors the same
idiom already used by `domain/pipelines/test/test_schema_service.py`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text

from app.core.auth import AuthMiddleware, Role
from app.core.identity import create_user, mint_session
from app.domain.artifacts import (
    ArtifactNotFoundError,
    get,
    latest_final,
    list_for_ticket,
    mark_final,
    store,
)
from app.domain.orgs import insert_membership, insert_org
from app.domain.tickets import create_from_pr

pytestmark = pytest.mark.service


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"artifacts"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _seed_org_ticket_run_and_stage(db_session) -> tuple[UUID, UUID, UUID, UUID]:
    org = await insert_org(db_session, slug=f"art-{uuid4().hex[:8]}")
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="artifacts test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    run_row = (
        await db_session.execute(
            text(
                "INSERT INTO pipeline_runs (org_id, ticket_id, pipeline_name, definition_snapshot, "
                "state, kickoff) VALUES (:org_id, :ticket_id, 'test-pipe', '{\"stages\": []}', "
                "'running', '{\"intake_point_id\": \"test\"}') RETURNING id"
            ),
            {"org_id": org.org_id, "ticket_id": ticket_id},
        )
    ).one()
    stage_row = (
        await db_session.execute(
            text(
                "INSERT INTO stage_executions (org_id, run_id, kind, stage_name, status) "
                "VALUES (:org_id, :run_id, 'skill', 'write-spec', 'running') RETURNING id"
            ),
            {"org_id": org.org_id, "run_id": run_row.id},
        )
    ).one()
    await db_session.flush()
    return org.org_id, ticket_id, run_row.id, stage_row.id


@pytest.mark.asyncio
async def test_store_version_sequencing(db_session) -> None:
    org_id, ticket_id, run_id, stage_execution_id = await _seed_org_ticket_run_and_stage(db_session)

    v1 = await store(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=run_id,
        stage_execution_id=stage_execution_id,
        stage_name="write-spec",
        body="draft one",
        iteration=0,
        session=db_session,
    )
    v2 = await store(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=run_id,
        stage_execution_id=stage_execution_id,
        stage_name="write-spec",
        body="draft two",
        iteration=1,
        session=db_session,
    )

    first = await get(v1, org_id=org_id, session=db_session)
    second = await get(v2, org_id=org_id, session=db_session)
    assert first.version == 1
    assert second.version == 2
    assert first.is_final is False


@pytest.mark.asyncio
async def test_mark_final_gates_latest_final(db_session) -> None:
    org_id, ticket_id, run_id, stage_execution_id = await _seed_org_ticket_run_and_stage(db_session)

    artifact_id = await store(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=run_id,
        stage_execution_id=stage_execution_id,
        stage_name="write-spec",
        body="draft",
        iteration=0,
        session=db_session,
    )

    assert (
        await latest_final(org_id=org_id, ticket_id=ticket_id, stage_name="write-spec", session=db_session)
        is None
    )

    await mark_final(artifact_id, session=db_session)

    final = await latest_final(
        org_id=org_id, ticket_id=ticket_id, stage_name="write-spec", session=db_session
    )
    assert final is not None
    assert final.id == artifact_id
    assert final.is_final is True


@pytest.mark.asyncio
async def test_get_unknown_id_raises(db_session) -> None:
    with pytest.raises(ArtifactNotFoundError):
        await get(uuid4(), org_id=uuid4(), session=db_session)


@pytest.mark.asyncio
async def test_list_for_ticket_groups_by_stage_name(db_session) -> None:
    org_id, ticket_id, run_id, stage_execution_id = await _seed_org_ticket_run_and_stage(db_session)

    await store(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=run_id,
        stage_execution_id=stage_execution_id,
        stage_name="write-spec",
        body="draft one",
        iteration=0,
        session=db_session,
    )
    await store(
        org_id=org_id,
        ticket_id=ticket_id,
        run_id=run_id,
        stage_execution_id=stage_execution_id,
        stage_name="write-spec",
        body="draft two",
        iteration=1,
        session=db_session,
    )

    groups = await list_for_ticket(org_id, ticket_id, session=db_session)
    assert len(groups) == 1
    assert groups[0].stage_name == "write-spec"
    assert [v.version for v in groups[0].versions] == [1, 2]


@pytest_asyncio.fixture
async def seeded(db_session):
    builder = await create_user(db_session, display_name="B")
    org = await insert_org(db_session, slug=f"art-http-{uuid4().hex[:8]}")
    await insert_membership(
        db_session, user_id=builder.id, org_id=org.org_id, role=Role.BUILDER, handle="bld"
    )
    builder_sess = await mint_session(db_session, user_id=builder.id, workspace_id=None)
    await db_session.commit()
    return {"org": org, "builder_sess": builder_sess}


def _headers(seeded) -> dict[str, str]:
    return {"X-Yaaos-Org-Slug": seeded["org"].slug}


def _cookies(seeded) -> dict[str, str]:
    return {
        "yaaos_session": seeded["builder_sess"].raw_token,
        "yaaos_csrf": seeded["builder_sess"].csrf_token,
    }


@pytest.mark.asyncio
async def test_http_list_and_get_artifact(seeded, db_session) -> None:
    org = seeded["org"]
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="http test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    run_row = (
        await db_session.execute(
            text(
                "INSERT INTO pipeline_runs (org_id, ticket_id, pipeline_name, definition_snapshot, "
                "state, kickoff) VALUES (:org_id, :ticket_id, 'test-pipe', '{\"stages\": []}', "
                "'running', '{\"intake_point_id\": \"test\"}') RETURNING id"
            ),
            {"org_id": org.org_id, "ticket_id": ticket_id},
        )
    ).one()
    stage_row = (
        await db_session.execute(
            text(
                "INSERT INTO stage_executions (org_id, run_id, kind, stage_name, status) "
                "VALUES (:org_id, :run_id, 'skill', 'write-spec', 'running') RETURNING id"
            ),
            {"org_id": org.org_id, "run_id": run_row.id},
        )
    ).one()
    artifact_id = await store(
        org_id=org.org_id,
        ticket_id=ticket_id,
        run_id=run_row.id,
        stage_execution_id=stage_row.id,
        stage_name="write-spec",
        body="the body",
        iteration=0,
        session=db_session,
    )
    await mark_final(artifact_id, session=db_session)
    await db_session.commit()

    async with _client() as c:
        listed = await c.get(
            "/api/artifacts",
            params={"ticket_id": str(ticket_id)},
            cookies=_cookies(seeded),
            headers=_headers(seeded),
        )
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["artifacts"][0]["stage_name"] == "write-spec"

        detail = await c.get(
            f"/api/artifacts/{artifact_id}", cookies=_cookies(seeded), headers=_headers(seeded)
        )
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert detail_body["body"] == "the body"
    assert detail_body["is_final"] is True


@pytest.mark.asyncio
async def test_http_get_unknown_artifact_404s(seeded) -> None:
    async with _client() as c:
        r = await c.get(f"/api/artifacts/{uuid4()}", cookies=_cookies(seeded), headers=_headers(seeded))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_cross_org_artifact_raises_not_found(db_session) -> None:
    """`get` must scope to `org_id` — a caller passing another org's id must
    not be able to read the artifact even if it knows the artifact_id."""
    org_a, ticket_id, run_id, stage_execution_id = await _seed_org_ticket_run_and_stage(db_session)
    org_b = await insert_org(db_session, slug=f"art-b-{uuid4().hex[:8]}")

    artifact_id = await store(
        org_id=org_a,
        ticket_id=ticket_id,
        run_id=run_id,
        stage_execution_id=stage_execution_id,
        stage_name="write-spec",
        body="org a's secret body",
        iteration=0,
        session=db_session,
    )

    with pytest.raises(ArtifactNotFoundError):
        await get(artifact_id, org_id=org_b.org_id, session=db_session)


@pytest.mark.asyncio
async def test_http_get_cross_org_artifact_404s(seeded, db_session) -> None:
    """A Builder in Org B must not be able to read Org A's artifact body via
    `GET /api/artifacts/<id>`, even though the endpoint's role check
    (`TICKETS_READ` in Org B) passes — the IDOR case."""
    org_a = seeded["org"]
    ticket_id, _ = await create_from_pr(
        org_id=org_a.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="cross-org test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    run_row = (
        await db_session.execute(
            text(
                "INSERT INTO pipeline_runs (org_id, ticket_id, pipeline_name, definition_snapshot, "
                "state, kickoff) VALUES (:org_id, :ticket_id, 'test-pipe', '{\"stages\": []}', "
                "'running', '{\"intake_point_id\": \"test\"}') RETURNING id"
            ),
            {"org_id": org_a.org_id, "ticket_id": ticket_id},
        )
    ).one()
    stage_row = (
        await db_session.execute(
            text(
                "INSERT INTO stage_executions (org_id, run_id, kind, stage_name, status) "
                "VALUES (:org_id, :run_id, 'skill', 'write-spec', 'running') RETURNING id"
            ),
            {"org_id": org_a.org_id, "run_id": run_row.id},
        )
    ).one()
    artifact_id = await store(
        org_id=org_a.org_id,
        ticket_id=ticket_id,
        run_id=run_row.id,
        stage_execution_id=stage_row.id,
        stage_name="write-spec",
        body="org a's secret body",
        iteration=0,
        session=db_session,
    )
    await mark_final(artifact_id, session=db_session)

    # Org B — a different org the caller is (also) a member of.
    org_b = await insert_org(db_session, slug=f"art-b-{uuid4().hex[:8]}")
    builder = await create_user(db_session, display_name="B2")
    await insert_membership(
        db_session, user_id=builder.id, org_id=org_b.org_id, role=Role.BUILDER, handle="bld-b"
    )
    builder_b_sess = await mint_session(db_session, user_id=builder.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        r = await c.get(
            f"/api/artifacts/{artifact_id}",
            cookies={
                "yaaos_session": builder_b_sess.raw_token,
                "yaaos_csrf": builder_b_sess.csrf_token,
            },
            headers={"X-Yaaos-Org-Slug": org_b.slug},
        )
    assert r.status_code == 404, r.text
