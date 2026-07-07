"""Service test: the nine new pipelines-engine tables accept a minimal insert,
and the `pipeline_runs` one-in-flight partial unique index rejects a second
concurrently-running run on one ticket.

This phase ships table shells only — no service behavior — so the test
exercises the schema directly. `domain/pipelines`' own four tables
(`pipelines`, `pipeline_runs`, `stage_executions`, `run_pauses`) are inserted
via their Row classes (intra-module test-dir carve-out — see patterns.md
§ Module boundaries in tests). The five tables owned by sibling modules
(`artifacts`, `pipeline_findings`, `repo_settings`, `repo_trigger_bindings`,
`pr_comments`) are seeded via raw SQL — the sanctioned test-file mechanism
for seeding cross-module state without a `*Row` cross-module import
(patterns.md § Module boundaries in tests; `bin/check_table_access` exempts
test files from the raw-SQL ownership scan for exactly this reason). None of
those modules' service functions exist yet this phase (stubs raise
`NotImplementedError`), so there is no public API path to drive instead.
"""

from __future__ import annotations

from uuid import uuid4, uuid7

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.tenancy import create_org
from app.domain.pipelines.models import PipelineRow, PipelineRunRow, RunPauseRow, StageExecutionRow
from app.domain.tickets import create_from_pr

pytestmark = pytest.mark.service


async def _seed_org_and_ticket(db_session):
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="schema test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.flush()
    return org.org_id, ticket_id


@pytest.mark.asyncio
async def test_pipelines_table_accepts_minimal_insert(db_session) -> None:
    org_id, _ = await _seed_org_and_ticket(db_session)
    row = PipelineRow(id=uuid7(), org_id=org_id, name="my-pipeline", stages=[])
    db_session.add(row)
    await db_session.flush()
    assert row.description == ""


@pytest.mark.asyncio
async def test_pipeline_runs_table_accepts_minimal_insert(db_session) -> None:
    org_id, ticket_id = await _seed_org_and_ticket(db_session)
    pipeline = PipelineRow(id=uuid7(), org_id=org_id, name="my-pipeline", stages=[])
    db_session.add(pipeline)
    await db_session.flush()

    run = PipelineRunRow(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=pipeline.id,
        pipeline_name=pipeline.name,
        definition_snapshot={"stages": []},
        state="running",
        kickoff={"intake_point_id": "github:pr_opened"},
    )
    db_session.add(run)
    await db_session.flush()
    assert run.phase == "provision"
    assert run.sendback_counts == {}


@pytest.mark.asyncio
async def test_pipeline_runs_one_in_flight_index_rejects_second_running_run(db_session) -> None:
    org_id, ticket_id = await _seed_org_and_ticket(db_session)
    pipeline = PipelineRow(id=uuid7(), org_id=org_id, name="my-pipeline", stages=[])
    db_session.add(pipeline)
    await db_session.flush()

    def _make_run(state: str) -> PipelineRunRow:
        return PipelineRunRow(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline.id,
            pipeline_name=pipeline.name,
            definition_snapshot={"stages": []},
            state=state,
            kickoff={"intake_point_id": "github:pr_opened"},
        )

    db_session.add(_make_run("running"))
    await db_session.flush()

    db_session.add(_make_run("running"))
    with pytest.raises(IntegrityError, match="ux_pipeline_runs_one_in_flight"):
        await db_session.flush()


@pytest.mark.asyncio
async def test_stage_executions_table_accepts_minimal_insert(db_session) -> None:
    org_id, ticket_id = await _seed_org_and_ticket(db_session)
    pipeline = PipelineRow(id=uuid7(), org_id=org_id, name="my-pipeline", stages=[])
    db_session.add(pipeline)
    await db_session.flush()
    run = PipelineRunRow(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=pipeline.id,
        pipeline_name=pipeline.name,
        definition_snapshot={"stages": []},
        state="running",
        kickoff={"intake_point_id": "github:pr_opened"},
    )
    db_session.add(run)
    await db_session.flush()

    stage_exec = StageExecutionRow(
        org_id=org_id,
        run_id=run.id,
        kind="system",
        stage_name="provision-workspace",
        status="running",
    )
    db_session.add(stage_exec)
    await db_session.flush()
    assert stage_exec.loop_state == []


@pytest.mark.asyncio
async def test_run_pauses_table_accepts_minimal_insert(db_session) -> None:
    org_id, ticket_id = await _seed_org_and_ticket(db_session)
    pipeline = PipelineRow(id=uuid7(), org_id=org_id, name="my-pipeline", stages=[])
    db_session.add(pipeline)
    await db_session.flush()
    run = PipelineRunRow(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=pipeline.id,
        pipeline_name=pipeline.name,
        definition_snapshot={"stages": []},
        state="paused",
        kickoff={"intake_point_id": "github:pr_opened"},
    )
    db_session.add(run)
    await db_session.flush()
    stage_exec = StageExecutionRow(
        org_id=org_id,
        run_id=run.id,
        kind="skill",
        stage_name="write-spec",
        status="completed",
    )
    db_session.add(stage_exec)
    await db_session.flush()

    pause = RunPauseRow(
        org_id=org_id,
        run_id=run.id,
        stage_execution_id=stage_exec.id,
        tripped={"mode": "always_hitl"},
        escalation_user_ids=[],
    )
    db_session.add(pause)
    await db_session.flush()
    assert pause.resolved_at is None


@pytest.mark.asyncio
async def test_artifacts_table_accepts_minimal_insert(db_session) -> None:
    org_id, ticket_id = await _seed_org_and_ticket(db_session)
    pipeline = PipelineRow(id=uuid7(), org_id=org_id, name="my-pipeline", stages=[])
    db_session.add(pipeline)
    await db_session.flush()
    run = PipelineRunRow(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=pipeline.id,
        pipeline_name=pipeline.name,
        definition_snapshot={"stages": []},
        state="running",
        kickoff={"intake_point_id": "github:pr_opened"},
    )
    db_session.add(run)
    await db_session.flush()
    stage_exec = StageExecutionRow(
        org_id=org_id,
        run_id=run.id,
        kind="skill",
        stage_name="write-spec",
        status="completed",
    )
    db_session.add(stage_exec)
    await db_session.flush()

    result = await db_session.execute(
        text(
            "INSERT INTO artifacts "
            "(org_id, ticket_id, stage_name, run_id, stage_execution_id, version, body) "
            "VALUES (:org_id, :ticket_id, 'write-spec', :run_id, :stage_execution_id, 1, 'body text') "
            "RETURNING id, is_final"
        ),
        {
            "org_id": org_id,
            "ticket_id": ticket_id,
            "run_id": run.id,
            "stage_execution_id": stage_exec.id,
        },
    )
    row = result.one()
    assert row.is_final is False


@pytest.mark.asyncio
async def test_pipeline_findings_table_accepts_minimal_insert(db_session) -> None:
    org_id, ticket_id = await _seed_org_and_ticket(db_session)

    result = await db_session.execute(
        text(
            "INSERT INTO pipeline_findings "
            "(id, org_id, ticket_id, source_run_id, source_stage_name, source_stage_execution_id, "
            " first_seen_iteration, display_prefix, display_id, severity, body) "
            "VALUES (uuidv7(), :org_id, :ticket_id, :run_id, 'write-spec', :stage_execution_id, "
            " 0, 'SPEC', 1, 'nit', 'a nit finding') "
            "RETURNING id, status"
        ),
        {
            "org_id": org_id,
            "ticket_id": ticket_id,
            "run_id": uuid4(),
            "stage_execution_id": uuid4(),
        },
    )
    row = result.one()
    assert row.status == "open"


@pytest.mark.asyncio
async def test_repo_settings_table_accepts_minimal_insert(db_session) -> None:
    org_id, _ = await _seed_org_and_ticket(db_session)

    result = await db_session.execute(
        text(
            "INSERT INTO repo_settings (org_id, repo_external_id) "
            "VALUES (:org_id, 'acme/repo') RETURNING protected_mode, auto_approve_enabled"
        ),
        {"org_id": org_id},
    )
    row = result.one()
    assert row.protected_mode == "deny"
    assert row.auto_approve_enabled is False


@pytest.mark.asyncio
async def test_repo_trigger_bindings_table_accepts_minimal_insert(db_session) -> None:
    org_id, _ = await _seed_org_and_ticket(db_session)
    pipeline = PipelineRow(id=uuid7(), org_id=org_id, name="my-pipeline", stages=[])
    db_session.add(pipeline)
    await db_session.flush()

    result = await db_session.execute(
        text(
            "INSERT INTO repo_trigger_bindings (org_id, repo_external_id, intake_point_id, pipeline_id) "
            "VALUES (:org_id, 'acme/repo', 'github:pr_opened', :pipeline_id) RETURNING id"
        ),
        {"org_id": org_id, "pipeline_id": pipeline.id},
    )
    assert result.one().id is not None


@pytest.mark.asyncio
async def test_pr_comments_table_accepts_minimal_insert(db_session) -> None:
    org_id, ticket_id = await _seed_org_and_ticket(db_session)

    result = await db_session.execute(
        text(
            "INSERT INTO pr_comments (org_id, ticket_id, comment_external_id, author_login, body) "
            "VALUES (:org_id, :ticket_id, :comment_external_id, 'someuser', 'looks good') "
            "RETURNING id, classification"
        ),
        {
            "org_id": org_id,
            "ticket_id": ticket_id,
            "comment_external_id": f"comment-{uuid4().hex[:8]}",
        },
    )
    row = result.one()
    assert row.classification is None
