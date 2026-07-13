"""Service tests for the `seed-inputs` system stage.

Acceptance flow: `start_manual_run` on a ticket with attachments →
provision system stage → `seed-inputs` system stage: a `WriteFiles`
AgentCommand is enqueued with one entry per attachment at
`.yaaos-inputs/<filename>` plus a `.yaaos-inputs/.gitignore`; on its
terminal success the engine advances to the first user stage.

Covers:
- `WriteFiles` command enqueued post-provision with correct entries.
- `.yaaos-inputs/.gitignore` always written.
- Kickoff carries `attachment_ids` snapshot.
- Run without attachments skips the `seed-inputs` stage entirely.
- `seed-inputs` failure → run reaches `failed` (via cleanup).
- Skill-stage `StageInvocationContext.attachments` is populated and the
  rendered prompt carries `## Attachments` with `role="context"`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import AgentEvent, AgentEventKind, record_agent_event
from app.core.agent_gateway import Artifact as WireArtifact
from app.core.audit_log import Actor, ActorKind
from app.core.auth import org_context
from app.core.tenancy import create_org
from app.core.workspace import is_workspace_provider_registered, register_workspace_providers
from app.domain.attachments import add_attachment, get_attachment
from app.domain.pipelines import (
    BoundaryControl,
    Kickoff,
    PipelineDefinition,
    SkillStage,
    create_pipeline,
)
from app.domain.pipelines.models import PipelineRunRow
from app.domain.pipelines.service import start_manual_run
from app.domain.pipelines.stage_prompt import render_stage_prompt
from app.domain.pipelines.test.drain import drain
from app.domain.pipelines.types import AttachmentRef, StageInvocationContext
from app.domain.tickets import create_from_manual
from app.testing.e2e_setup import seed_agent
from app.testing.stub_vcs import register_stub_vcs

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("redis_or_skip")]

_STAGE_NAME = "write-spec"


def _one_skill_stage_definition() -> PipelineDefinition:
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=(
            SkillStage(
                name=_STAGE_NAME,
                skill_name=_STAGE_NAME,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=BoundaryControl(mode="always_proceed"),
            ),
        ),
    )


async def _seed_org_ticket(db_session: AsyncSession) -> tuple[UUID, UUID]:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    ticket_id, _ = await create_from_manual(
        org_id=org.org_id,
        title="seed-inputs test ticket",
        repo_external_id="acme/repo",
        actor=Actor.system(),
        session=db_session,
    )
    # `create_from_manual` mints a `yaaos/…` branch name; provision reads
    # it from the row, so update it to a stable name for test predictability.
    await db_session.execute(
        text("UPDATE tickets SET branch_name = :b WHERE id = :id"),
        {"b": "main", "id": ticket_id},
    )
    await db_session.flush()
    return org.org_id, ticket_id


def _success_event(command_id: UUID, *, outputs: dict, artifact_body: str | None = None) -> AgentEvent:
    return AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs=outputs,
        reported_at=datetime.now(UTC),
        traceparent="",
        artifact=WireArtifact(body=artifact_body) if artifact_body is not None else None,
    )


def _failure_event(command_id: UUID, *, reason: str) -> AgentEvent:
    return AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_FAILURE,
        outcome_label=reason,
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
    )


async def _record(
    org_id: UUID, event: AgentEvent, *, agent_id: UUID | None, db_session: AsyncSession
) -> None:
    async with org_context(org_id, ActorKind.WORKSPACE, actor_id=None):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.commit()


async def _advance_to_seed_inputs_dispatch(
    db_session: AsyncSession,
) -> tuple[UUID, UUID, UUID, UUID, list[UUID]]:
    """Drive to the `seed-inputs` stage: provision done, write-files command parked.

    Seeds two attachments on the ticket, starts the run, drives provision to
    success, and returns once the run is parked on the `WriteFiles` command.

    Returns `(org_id, ticket_id, run_id, write_files_command_id, [att_id_1, att_id_2])`.
    """
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id = await _seed_org_ticket(db_session)
        agent_row = await seed_agent(org_id=org_id)

        att1 = await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="spec.md",
            body="# Spec\n\nrequirements here",
            note="initial spec",
            actor=Actor.system(),
            session=db_session,
        )
        att2 = await add_attachment(
            ticket_id,
            org_id=org_id,
            filename="notes.txt",
            body="extra notes",
            note=None,
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_one_skill_stage_definition(),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        run_id = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="go write the spec",
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.phase == "provision"
        provision_command_id = run.pending_agent_command_id
        assert provision_command_id is not None

        await _record(
            org_id,
            _success_event(provision_command_id, outputs={}),
            agent_id=agent_row["id"],
            db_session=db_session,
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.phase == "stages"
        write_files_command_id = run.pending_agent_command_id
        assert write_files_command_id is not None

        return org_id, ticket_id, run_id, write_files_command_id, [att1.id, att2.id]


@pytest.mark.asyncio
async def test_write_files_command_enqueued_with_correct_entries(db_session: AsyncSession) -> None:
    """A WriteFiles AgentCommand is enqueued after provision with one entry per
    attachment plus `.yaaos-inputs/.gitignore`."""
    _, _, _, write_files_command_id, _ = await _advance_to_seed_inputs_dispatch(db_session)

    row = (
        await db_session.execute(
            text("SELECT command_kind, payload FROM agent_commands WHERE id = :id"),
            {"id": write_files_command_id},
        )
    ).one()

    assert row.command_kind == "WriteFiles"
    files: list[dict] = row.payload["files"]
    paths = {f["path"] for f in files}
    assert ".yaaos-inputs/spec.md" in paths
    assert ".yaaos-inputs/notes.txt" in paths
    assert ".yaaos-inputs/.gitignore" in paths

    spec_entry = next(f for f in files if f["path"] == ".yaaos-inputs/spec.md")
    assert spec_entry["content"] == "# Spec\n\nrequirements here"
    notes_entry = next(f for f in files if f["path"] == ".yaaos-inputs/notes.txt")
    assert notes_entry["content"] == "extra notes"
    gitignore_entry = next(f for f in files if f["path"] == ".yaaos-inputs/.gitignore")
    assert gitignore_entry["content"] == "*\n"


@pytest.mark.asyncio
async def test_kickoff_carries_attachment_ids_snapshot(db_session: AsyncSession) -> None:
    """The run's kickoff stores the attachment ids at run-start time."""
    _, _, run_id, _, att_ids = await _advance_to_seed_inputs_dispatch(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    kickoff = Kickoff.model_validate(run.kickoff)
    assert set(kickoff.attachment_ids) == set(att_ids)


@pytest.mark.asyncio
async def test_seed_inputs_stage_row_created(db_session: AsyncSession) -> None:
    """`seed-inputs` system stage execution row is created and running."""
    _, _, run_id, _, _ = await _advance_to_seed_inputs_dispatch(db_session)

    rows = (
        await db_session.execute(
            text(
                "SELECT kind, stage_name, status FROM stage_executions "
                "WHERE run_id = :run_id AND stage_name = 'seed-inputs'"
            ),
            {"run_id": run_id},
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].kind == "system"
    assert rows[0].status == "running"


@pytest.mark.asyncio
async def test_seed_inputs_success_advances_to_skill_stage(db_session: AsyncSession) -> None:
    """After `seed-inputs` terminal success the engine dispatches the first skill stage."""
    org_id, _, run_id, write_files_command_id, _ = await _advance_to_seed_inputs_dispatch(db_session)

    await _record(
        org_id,
        _success_event(write_files_command_id, outputs={}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.phase == "stages"
    skill_command_id = run.pending_agent_command_id
    assert skill_command_id is not None

    skill_row = (
        await db_session.execute(
            text("SELECT command_kind FROM agent_commands WHERE id = :id"),
            {"id": skill_command_id},
        )
    ).one()
    assert skill_row.command_kind == "InvokeClaudeCode"


@pytest.mark.asyncio
async def test_seed_inputs_failure_fails_run(db_session: AsyncSession) -> None:
    """`seed-inputs` terminal failure transitions the run to `failed`."""
    org_id, _, run_id, write_files_command_id, _ = await _advance_to_seed_inputs_dispatch(db_session)

    await _record(
        org_id,
        _failure_event(write_files_command_id, reason="disk full"),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    # Failure routes through cleanup first.
    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.phase == "cleanup"
    cleanup_command_id = run.pending_agent_command_id
    assert cleanup_command_id is not None

    await _record(
        org_id,
        _success_event(cleanup_command_id, outputs={}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.state == "failed"
    assert "disk full" in (run.failure_reason or "")


@pytest.mark.asyncio
async def test_no_seed_inputs_when_no_attachments(db_session: AsyncSession) -> None:
    """When the ticket has no attachments the engine skips `seed-inputs` and
    goes directly to the first skill stage after provision."""
    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    with register_stub_vcs(plugin_id="github"):
        org_id, ticket_id = await _seed_org_ticket(db_session)
        agent_row = await seed_agent(org_id=org_id)

        # No attachments added.
        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_one_skill_stage_definition(),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        run_id = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text=None,
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        provision_command_id = run.pending_agent_command_id
        assert provision_command_id is not None

        await _record(
            org_id,
            _success_event(provision_command_id, outputs={}),
            agent_id=agent_row["id"],
            db_session=db_session,
        )
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.phase == "stages"
        skill_command_id = run.pending_agent_command_id
        assert skill_command_id is not None

        # Confirm the next command is InvokeClaudeCode, not WriteFiles.
        skill_row = (
            await db_session.execute(
                text("SELECT command_kind FROM agent_commands WHERE id = :id"),
                {"id": skill_command_id},
            )
        ).one()
        assert skill_row.command_kind == "InvokeClaudeCode"

        # No `seed-inputs` stage execution row created.
        seed_rows = (
            await db_session.execute(
                text("SELECT id FROM stage_executions WHERE run_id = :run_id AND stage_name = 'seed-inputs'"),
                {"run_id": run_id},
            )
        ).all()
        assert len(seed_rows) == 0


@pytest.mark.asyncio
async def test_skill_stage_prompt_contains_attachments_section(db_session: AsyncSession) -> None:
    """The skill-stage `StageInvocationContext` includes `attachments` entries
    and the rendered prompt carries `## Attachments` with `role="context"`.

    Because the stub coding agent discards the rendered prompt, we verify
    the manifest by:
    1. Confirming `kickoff.attachment_ids` was snapshotted (proven by the
       WriteFiles dispatch test above).
    2. Building `StageInvocationContext.attachments` refs from the kickoff
       snapshot (the same path the engine takes).
    3. Rendering the prompt from a `StageInvocationContext` carrying those
       refs and asserting the `## Attachments` section is present.
    """
    org_id, ticket_id, run_id, write_files_command_id, att_ids = await _advance_to_seed_inputs_dispatch(
        db_session
    )

    # Advance past seed-inputs to the skill stage.
    await _record(
        org_id,
        _success_event(write_files_command_id, outputs={}),
        agent_id=None,
        db_session=db_session,
    )
    await drain(db_session)

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    kickoff = Kickoff.model_validate(run.kickoff)
    assert set(kickoff.attachment_ids) == set(att_ids)

    # Reconstruct the attachment refs the engine would have built.
    refs: list[AttachmentRef] = []
    for att_id in kickoff.attachment_ids:
        att = await get_attachment(att_id, org_id=org_id, session=db_session)
        refs.append(
            AttachmentRef(
                path=f".yaaos-inputs/{att.filename}",
                artifact_type=att.artifact_type,
                produced_by_skill=att.produced_by_skill,
                role="context",
                note=att.note,
            )
        )

    # Build and render a representative StageInvocationContext.
    ctx = StageInvocationContext(
        ticket_id=ticket_id,
        stage_name=_STAGE_NAME,
        branch_name="main",
        input="go write the spec",
        attachments=tuple(refs),
        artifact_path="$TMPDIR/test.md",
    )
    prompt = render_stage_prompt(
        ctx.model_dump(mode="json"),
        skill_directive='Use the "write-spec" skill.',
    )

    assert "## Attachments" in prompt
    assert "role: context" in prompt
    assert ".yaaos-inputs/spec.md" in prompt
    assert ".yaaos-inputs/notes.txt" in prompt
