"""enumerate_skills_v1 workflow + EnumerateSkills + PersistSkillManifest WorkflowCommands.

`enumerate_skills_v1` runs four steps:
  1. ProvisionWorkspace (Workspace) — clone the repo.
  2. EnumerateSkills (Workspace) — scan skills via the AgentCommand.
  3. PersistSkillManifest (Local) — write manifest to DB + emit SSE.
  4. CleanupWorkspace (Workspace) — tear down the clone.

`finalizer_step_id="cleanup"` ensures CleanupWorkspace always runs, even when
an earlier step fails. PersistSkillManifest is Local so it runs inline and
has access to the step_state outputs from the prior agent step.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import structlog

from app.core.agent_gateway import EnumerateSkillsCommand, enqueue_command, pin_command_to_agent
from app.core.sse import GeneralEventKind, publish_general_after_commit
from app.core.workflow import CommandCategory, CommandContext, Outcome, Step, TerminalAction, Workflow
from app.core.workspace import WorkspaceOwner, get_workflow_context_provider, get_workspace_owner
from app.plugins.claude_code.repos import SkillManifestEntry, persist_skill_manifest

log = structlog.get_logger("claude_code.enumerate_workflow")


class EnumerateSkills:
    """Workspace-category WorkflowCommand. Dispatches an `EnumerateSkills`
    AgentCommand pinned to the provisioned workspace's owning agent.

    `inputs["workspace_id"]` comes from the prior ProvisionWorkspace step's
    outputs via the workflow `$provision.workspace_id` input expression.
    """

    kind = "EnumerateSkills"
    category = CommandCategory.WORKSPACE
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        """Inline execution — only used in unit tests that bypass dispatch."""
        del inputs, ctx
        return Outcome.success()

    async def dispatch(
        self,
        inputs: dict[str, Any],
        ctx: CommandContext,
        *,
        session: Any,
    ) -> UUID:
        """Enqueue an `EnumerateSkills` AgentCommand pinned to the workspace's
        owning agent. Returns the new `command_id`."""
        ws_id_raw = inputs.get("workspace_id")
        if not ws_id_raw:
            raise RuntimeError("EnumerateSkills.dispatch missing workspace_id input")
        ws_id = UUID(str(ws_id_raw))

        owner: WorkspaceOwner | None = await get_workspace_owner(ws_id, session)
        if owner is None:
            raise RuntimeError(f"workspace {ws_id} not found for EnumerateSkills.dispatch")

        command_id = uuid4()
        cmd = EnumerateSkillsCommand(
            command_id=command_id,
            workspace_id=ws_id,
            traceparent=ctx.traceparent or "",
        )
        await enqueue_command(
            org_id=owner.org_id,
            command=cmd,
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
        )
        if owner.owning_agent_id is not None:
            await pin_command_to_agent(command_id, owner.owning_agent_id, session=session)

        log.info(
            "enumerate_skills.dispatched",
            workflow_execution_id=ctx.workflow_execution_id,
            workspace_id=str(ws_id),
            command_id=str(command_id),
        )
        return command_id


class PersistSkillManifest:
    """Local WorkflowCommand that persists the skill manifest from the prior
    EnumerateSkills step's outputs and fires the `skills_enumerated` SSE event.

    `inputs["skills"]` is the raw list from `EnumerateSkills` agent outputs.
    The ticket's `repo_external_id` and `org_id` are resolved via the
    `WorkflowContextProvider` using the ticket id on the CommandContext.
    """

    kind = "PersistSkillManifest"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        from app.core.database import session as db_session  # noqa: PLC0415

        async with db_session() as s:
            ctx_provider = get_workflow_context_provider()
            ticket_ctx = await ctx_provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
            if ticket_ctx is None:
                return Outcome.failure(reason=f"ticket {ctx.ticket_id} not found")

            raw_skills = inputs.get("skills") or []
            skills: list[SkillManifestEntry] = []
            for raw in raw_skills:
                try:
                    skills.append(SkillManifestEntry.model_validate(raw))
                except Exception:
                    log.warning(
                        "persist_skill_manifest.invalid_entry",
                        entry=raw,
                        ticket_id=ctx.ticket_id,
                    )

            await persist_skill_manifest(
                ticket_ctx.org_id,
                ticket_ctx.repo_external_id,
                skills,
                session=s,
            )

            publish_general_after_commit(
                s,
                org_id=ticket_ctx.org_id,
                kind=GeneralEventKind.SKILLS_ENUMERATED,
                payload={"repo_external_id": ticket_ctx.repo_external_id},
            )

            await s.commit()

        log.info(
            "persist_skill_manifest.done",
            ticket_id=ctx.ticket_id,
            skill_count=len(skills),
        )
        return Outcome.success(outputs={"skill_count": len(skills)})


def build_enumerate_skills_workflow() -> Workflow:
    """Build and return the `enumerate_skills_v1` Workflow definition.

    Steps: ProvisionWorkspace → EnumerateSkills → PersistSkillManifest → CleanupWorkspace.
    `finalizer_step_id="cleanup"` ensures teardown runs even on failure.
    """
    return Workflow(
        name="enumerate_skills_v1",
        version=1,
        entry_step_id="provision",
        finalizer_step_id="cleanup",
        steps=(
            Step(
                id="provision",
                command_kind="ProvisionWorkspace",
                transitions={
                    "success": "enumerate",
                    "failure": TerminalAction.FAIL_WORKFLOW,
                },
            ),
            Step(
                id="enumerate",
                command_kind="EnumerateSkills",
                inputs={"workspace_id": "$provision.workspace_id"},
                transitions={
                    "success": "persist_manifest",
                    "failure": TerminalAction.FAIL_WORKFLOW,
                },
            ),
            Step(
                id="persist_manifest",
                command_kind="PersistSkillManifest",
                inputs={
                    "workspace_id": "$provision.workspace_id",
                    "skills": "$enumerate.skills",
                },
                transitions={
                    "success": "cleanup",
                    "failure": "cleanup",
                },
            ),
            Step(
                id="cleanup",
                command_kind="CleanupWorkspace",
                inputs={"workspace_id": "$provision.workspace_id"},
                transitions={
                    "success": TerminalAction.COMPLETE_WORKFLOW,
                    "failure": TerminalAction.COMPLETE_WORKFLOW,
                },
            ),
        ),
    )
