"""Pure-data helpers behind the `/api/testing` HTTP surface.

These are split out of `web.py` so backend integration tests can call them
directly without going through HTTP. The functions are idempotent where it
makes sense (truncate, ensure-builtin-agents); seeders that insert specific
rows fail if the row already exists, surfacing programmer error instead of
silently no-op'ing.

Imports for every module that owns tables happen at the top of this file so
`Base.metadata.sorted_tables` reflects the full schema regardless of which
HTTP routes have been mounted in the calling process.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4, uuid7

# Trigger-imports: importing each module's __init__ ensures `Base.metadata` is
# fully populated so `truncate_all_tables` sees the complete schema regardless
# of which HTTP routes have been mounted in the calling process. We import any
# public symbol (not a Row) — the side effect of the import is what matters.
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.database import truncate_all_tables

# The whole codebase pins org_id to this constant in . Same value the
# domain modules use as the system-actor org.
DEFAULT_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")


async def reset() -> None:
    """Truncate all tables, flush Redis rate-limit state, and clear the email inbox.

    DB truncation covers all domain state. Redis rate-limit keys for the
    agent identity-exchange endpoint are also deleted so a subsequent seed
    (bootstrap_owner) isn't blocked by a prior run's burst from the agent
    container's IP. The module-global email inbox is cleared so emails from a
    previous (possibly failed) test run do not pollute the next run.
    """
    from app.core.agent_gateway import delete_identity_exchange_rate_limits  # noqa: PLC0415
    from app.domain.orgs import clear_global_inbox  # noqa: PLC0415

    async with db_session() as s:
        await truncate_all_tables(s)
        await s.commit()
    await delete_identity_exchange_rate_limits()
    clear_global_inbox()


async def seed_github_install(
    *,
    org_login: str = "acme",
    target_org_slug: str | None = None,
) -> None:
    """Seed an active ``github_app_installations`` row + a Claude Code settings
    row on the chosen org. Pre-populates the post-install state so specs that
    aren't about the install handshake itself can skip it.

    ``org_login`` is the GitHub-side ``account_login`` on the install row.
    ``target_org_slug``, when provided, picks the yaaos-side org row to attach
    the rows to (looked up by slug); otherwise the ``DEFAULT_ORG_ID`` stub
    is used. Specs that also log a user in via ``bootstrap_owner`` pass the
    bootstrapped org's slug here so the install lives on the same org as the
    authenticated user — ``/org/<slug>/tickets`` then surfaces webhook-created
    tickets under the route the user is on.

    The platform GitHub App credentials come from ``yaaos_github_app_*`` env
    vars (set on the test compose); no per-org credential row is needed.

    Deliberate side-effect: this seed path calls public service functions
    (``github.record_app_install``, ``byok.set``,
    ``orgs.install_coding_agent``) so it emits the same audit rows and events
    that production writes would produce.
    """
    import app.core.byok as byok_service  # noqa: PLC0415
    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.domain.orgs import get_org_by_slug, install_coding_agent  # noqa: PLC0415
    from app.plugins.github import record_app_install  # noqa: PLC0415

    if target_org_slug is not None:
        org = await get_org_by_slug(target_org_slug)
        if org is None:
            raise ValueError(f"org {target_org_slug!r} not found — seed it first via bootstrap_owner")
        target_org_id = org.id
    else:
        target_org_id = DEFAULT_ORG_ID
    async with db_session() as s:
        await record_app_install(
            s,
            org_id=target_org_id,
            install_external_id="fake-install-1",
            account_login=org_login,
        )
        await byok_service.set(
            target_org_id,
            "anthropic",
            "TEST-FAKE-NOT-FOR-PROD-ANTHROPIC-KEY",
            actor=Actor.system(),
            session=s,
        )
        # also write the OrgCodingAgentRow so the bespoke Coding Agent
        # settings page (claude_code's AgentEditor) renders against the
        # configured defaults instead of an empty-state placeholder.
        await install_coding_agent(
            s,
            org_id=target_org_id,
            plugin_id="claude_code",
            settings={},
            actor=Actor.system(),
        )
        await s.commit()


async def seed_repo_skill(*, org_slug: str, repo_external_id: str, skill_name: str) -> None:
    """Write `skill_name` for a connected repo via the direct service-layer call.

    Used by e2e specs that render the Code Connect settings page and expect a
    non-null skill_name for the repo. No pipeline dispatch path reads this
    field today — a pipeline stage's own `skill_name` is the mechanism that
    picks a skill. The seed exists for SPA read-back assertions only.
    """
    from app.domain.orgs import get_org_by_slug  # noqa: PLC0415
    from app.plugins.claude_code import set_repo_skill  # noqa: PLC0415

    org = await get_org_by_slug(org_slug)
    if org is None:
        raise ValueError(f"org {org_slug!r} not found — seed it first via bootstrap_owner")
    async with db_session() as s:
        await set_repo_skill(org.id, repo_external_id, skill_name, session=s)
        await s.commit()


async def seed_pipeline(*, org_id: UUID, name: str, action_id: str = "github:create_pr") -> UUID:
    """Insert a minimal one-stage pipeline via the public
    ``domain.pipelines.create_pipeline`` service. ``action_id`` need not be a
    registered action — this seed only exercises trigger-binding + run
    creation (`pipelines.start_run` succeeds synchronously regardless of
    whether a later stage dispatch can resolve the action), not stage
    execution.
    """
    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.domain.pipelines import ActionStage, PipelineDefinition, create_pipeline  # noqa: PLC0415

    async with db_session() as s:
        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=PipelineDefinition(
                name=name, description="", stages=(ActionStage(action_id=action_id),)
            ),
            actor=Actor.system(),
            session=s,
        )
        await s.commit()
    return pipeline_id


async def seed_trigger_binding(
    *, org_id: UUID, repo_external_id: str, intake_point_id: str, pipeline_id: UUID
) -> UUID:
    """Insert a repo trigger binding via the public ``domain.repos.add_binding``
    service. Returns the binding id."""
    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.domain.repos import TriggerBindingSpec, add_binding  # noqa: PLC0415

    async with db_session() as s:
        binding_id = await add_binding(
            org_id,
            repo_external_id,
            spec=TriggerBindingSpec(intake_point_id=intake_point_id, pipeline_id=pipeline_id),
            actor=Actor.system(),
            session=s,
        )
        await s.commit()
    return binding_id


async def seed_paused_run(
    *, org_slug: str, ticket_title: str, stage_name: str = "write-spec"
) -> dict[str, str]:
    """Seed a `pipeline_runs` row already `paused` at an `always_hitl`
    boundary, with a stored final artifact and an open `run_pauses` row —
    bypassing the run engine's coding-agent dispatch entirely (no invocation
    is compiled, no workspace is provisioned, no real agent is involved).

    Lets e2e specs exercise the ticket page's Overview attention block and
    `POST /api/pipelines/runs/pauses/{id}/respond` without depending on a
    live coding-agent completing a real skill invocation — faster and
    deterministic for specs that only need a paused-run fixture.
    `escalation_user_ids` is left empty; the responding org admin/owner
    authorizes via `is_pause_responder`'s admin-union clause, not escalation
    membership.

    Row shapes mirror exactly what the engine itself writes on a real
    always-HITL pause (see `domain/pipelines/test/test_boundary_pause_service.py`
    `_advance_to_paused` + `engine._handle_main_return`/`_enter_pause`), via
    raw SQL to avoid importing pipelines' Row types across the module
    boundary. Returns `{"ticket_id", "run_id", "pause_id", "stage_execution_id"}`.
    """
    import json as _json  # noqa: PLC0415

    from sqlalchemy import text as sa_text  # noqa: PLC0415

    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.domain.artifacts import mark_final, store  # noqa: PLC0415
    from app.domain.orgs import get_org_by_slug  # noqa: PLC0415
    from app.domain.pipelines import (  # noqa: PLC0415
        BoundaryControl,
        Kickoff,
        PipelineDefinition,
        SkillStage,
        create_pipeline,
    )
    from app.domain.tickets import create_from_pr  # noqa: PLC0415

    org = await get_org_by_slug(org_slug)
    if org is None:
        raise ValueError(f"org {org_slug!r} not found — seed it first via bootstrap_owner")

    definition = PipelineDefinition(
        name=f"seed-pipe-{uuid4().hex[:8]}",
        stages=(
            SkillStage(
                name=stage_name,
                skill_name=stage_name,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=BoundaryControl(),  # default mode="always_hitl"
            ),
        ),
    )
    stage = definition.stages[0]

    async with db_session() as s:
        pipeline_id = await create_pipeline(
            org_id=org.id, definition=definition, actor=Actor.system(), session=s
        )
        ticket_id, _ = await create_from_pr(
            org_id=org.id,
            source_external_id=f"seed-paused-run-{uuid4().hex[:8]}",
            title=ticket_title,
            description=None,
            repo_external_id="acme/repo",
            plugin_id="github",
            idempotency_key=f"key-{uuid4().hex}",
            payload={},
            session=s,
            branch_name="yaaos/seed-paused-run",
        )
        await s.execute(
            sa_text("UPDATE tickets SET status = 'hitl' WHERE id = :id"),
            {"id": ticket_id},
        )

        run_id = uuid4()
        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text="spec please")
        await s.execute(
            sa_text(
                "INSERT INTO pipeline_runs"
                " (id, org_id, ticket_id, pipeline_id, pipeline_name, definition_snapshot,"
                "  state, phase, current_stage_index, kickoff)"
                " VALUES (:id, :org_id, :ticket_id, :pipeline_id, :pipeline_name,"
                "  cast(:snapshot as jsonb), 'paused', 'stages', 0, cast(:kickoff as jsonb))"
            ),
            {
                "id": run_id,
                "org_id": org.id,
                "ticket_id": ticket_id,
                "pipeline_id": pipeline_id,
                "pipeline_name": definition.name,
                "snapshot": _json.dumps({"stages": [stage.model_dump(mode="json")]}),
                "kickoff": kickoff.model_dump_json(),
            },
        )
        await s.execute(
            sa_text("UPDATE tickets SET current_run_id = :run_id WHERE id = :id"),
            {"run_id": run_id, "id": ticket_id},
        )

        stage_exec_id = uuid4()
        artifact_body = "# Spec\n\nSeeded artifact body for e2e."
        await s.execute(
            sa_text(
                "INSERT INTO stage_executions"
                " (id, org_id, run_id, stage_index, kind, stage_name, skill_name, status,"
                "  confidence, boundary_outcome, completed_at)"
                " VALUES (:id, :org_id, :run_id, 0, 'skill', :stage_name, :stage_name,"
                "  'completed', 'high', 'paused', now())"
            ),
            {"id": stage_exec_id, "org_id": org.id, "run_id": run_id, "stage_name": stage_name},
        )
        artifact_id = await store(
            org_id=org.id,
            ticket_id=ticket_id,
            run_id=run_id,
            stage_execution_id=stage_exec_id,
            stage_name=stage_name,
            body=artifact_body,
            iteration=0,
            session=s,
        )
        await mark_final(artifact_id, session=s)
        await s.execute(
            sa_text("UPDATE stage_executions SET loop_state = cast(:loop_state as jsonb) WHERE id = :id"),
            {
                "loop_state": _json.dumps(
                    [
                        {
                            "phase": "main",
                            "artifact_id": str(artifact_id),
                            "confidence": 90,
                            "paths_affected": [],
                        }
                    ]
                ),
                "id": stage_exec_id,
            },
        )

        pause_id = uuid4()
        await s.execute(
            sa_text(
                "INSERT INTO run_pauses"
                " (id, org_id, run_id, stage_execution_id, tripped, escalation_user_ids)"
                " VALUES (:id, :org_id, :run_id, :stage_execution_id, cast(:tripped as jsonb),"
                "  ARRAY[]::uuid[])"
            ),
            {
                "id": pause_id,
                "org_id": org.id,
                "run_id": run_id,
                "stage_execution_id": stage_exec_id,
                "tripped": _json.dumps({"always_hitl": True}),
            },
        )
        await s.commit()

    return {
        "ticket_id": str(ticket_id),
        "run_id": str(run_id),
        "pause_id": str(pause_id),
        "stage_execution_id": str(stage_exec_id),
    }


async def seed_running_run(
    *, org_slug: str, ticket_title: str, stage_name: str = "write-code"
) -> dict[str, str]:
    """Seed a `pipeline_runs` row in `running` state with one skill
    `stage_executions` row in `running` status — bypassing the run engine
    entirely (no invocation compiled, no workspace provisioned, no real agent).

    Lets e2e specs exercise the ticket page's Runs-tab live activity pane and
    Overview in-flight card without depending on a live coding-agent. Row shapes
    mirror what the engine writes when it enters a skill stage (status='running',
    no artifact, no pause). Returns `{"ticket_id", "run_id", "stage_execution_id"}`.
    """
    import json as _json  # noqa: PLC0415

    from sqlalchemy import text as sa_text  # noqa: PLC0415

    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.domain.orgs import get_org_by_slug  # noqa: PLC0415
    from app.domain.pipelines import (  # noqa: PLC0415
        BoundaryControl,
        Kickoff,
        PipelineDefinition,
        SkillStage,
        create_pipeline,
    )
    from app.domain.tickets import create_from_pr  # noqa: PLC0415

    org = await get_org_by_slug(org_slug)
    if org is None:
        raise ValueError(f"org {org_slug!r} not found — seed it first via bootstrap_owner")

    definition = PipelineDefinition(
        name=f"seed-pipe-{uuid4().hex[:8]}",
        stages=(
            SkillStage(
                name=stage_name,
                skill_name=stage_name,
                coding_agent_plugin_id="claude_code",
                model="sonnet",
                effort="medium",
                boundary=BoundaryControl(),
            ),
        ),
    )
    stage = definition.stages[0]

    async with db_session() as s:
        pipeline_id = await create_pipeline(
            org_id=org.id, definition=definition, actor=Actor.system(), session=s
        )
        ticket_id, _ = await create_from_pr(
            org_id=org.id,
            source_external_id=f"seed-running-run-{uuid4().hex[:8]}",
            title=ticket_title,
            description=None,
            repo_external_id="acme/repo",
            plugin_id="github",
            idempotency_key=f"key-{uuid4().hex}",
            payload={},
            session=s,
            branch_name="yaaos/seed-running-run",
        )
        await s.execute(
            sa_text("UPDATE tickets SET status = 'running' WHERE id = :id"),
            {"id": ticket_id},
        )

        run_id = uuid4()
        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text="code it")
        await s.execute(
            sa_text(
                "INSERT INTO pipeline_runs"
                " (id, org_id, ticket_id, pipeline_id, pipeline_name, definition_snapshot,"
                "  state, phase, current_stage_index, kickoff)"
                " VALUES (:id, :org_id, :ticket_id, :pipeline_id, :pipeline_name,"
                "  cast(:snapshot as jsonb), 'running', 'stages', 0, cast(:kickoff as jsonb))"
            ),
            {
                "id": run_id,
                "org_id": org.id,
                "ticket_id": ticket_id,
                "pipeline_id": pipeline_id,
                "pipeline_name": definition.name,
                "snapshot": _json.dumps({"stages": [stage.model_dump(mode="json")]}),
                "kickoff": kickoff.model_dump_json(),
            },
        )
        await s.execute(
            sa_text("UPDATE tickets SET current_run_id = :run_id WHERE id = :id"),
            {"run_id": run_id, "id": ticket_id},
        )

        stage_exec_id = uuid4()
        await s.execute(
            sa_text(
                "INSERT INTO stage_executions"
                " (id, org_id, run_id, stage_index, kind, stage_name, skill_name, status)"
                " VALUES (:id, :org_id, :run_id, 0, 'skill', :stage_name, :stage_name, 'running')"
            ),
            {"id": stage_exec_id, "org_id": org.id, "run_id": run_id, "stage_name": stage_name},
        )
        await s.commit()

    return {
        "ticket_id": str(ticket_id),
        "run_id": str(run_id),
        "stage_execution_id": str(stage_exec_id),
        "org_id": str(org.id),
    }


async def seed_lesson(*, repo_external_id: str, title: str, body: str) -> UUID:
    """Insert a single lesson via the public ``lessons.create`` service.

    Returns the generated lesson id. Caller chooses the title so
    duplicate-title detection (if needed) lives in the spec, not here.

    Deliberate side-effect: ``lessons.create`` emits a ``lesson.created``
    audit row — matching what production writes produce.
    """
    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.domain.lessons import create as create_lesson  # noqa: PLC0415

    lesson = await create_lesson(
        repo_external_id,
        title,
        body,
        None,
        actor=Actor.system(),
        org_id=DEFAULT_ORG_ID,
        plugin_id="github",
    )
    return lesson.id


async def seed_broken_integration(*, org_slug: str, provider: str = "linear") -> None:
    """Seed an ``mcp_credentials`` row with ``last_refresh_status="failed"`` so e2e
    specs can exercise the broken-creds banner + Integrations settings page
    against a known org. Encrypts placeholder tokens via ``core/secrets``."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from app.core.secrets import encrypt  # noqa: PLC0415
    from app.domain.integrations import create_credential  # noqa: PLC0415
    from app.domain.orgs import get_org_by_slug  # noqa: PLC0415

    org = await get_org_by_slug(org_slug)
    if org is None:
        raise ValueError(f"org {org_slug!r} not found — seed it first via bootstrap_owner")
    async with db_session() as s:
        await create_credential(
            s,
            org_id=org.id,
            provider=provider,
            encrypted_access_token=encrypt("stub-access").decode(),
            encrypted_refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["read"],
            allowed_tools=[],
            enabled=True,
            upstream_identity=f"{provider}-bot",
            last_refresh_status="failed",
            last_refresh_failed_at=datetime.now(UTC),
        )
        await s.commit()


def is_dev_env() -> bool:
    """Gate used by every `/api/testing/*` route. Centralised so the rule
    `non-prod-only routes` lives in one place, not per-handler. True for
    `dev` and `test`; prod returns 404 via every gated handler.
    """
    return get_settings().is_non_prod


# ── auth-flow helpers ──────────────────────────────────────────────


async def seed_bootstrap_owner(
    *,
    email: str,
    github_id: str,
    org_slug: str,
    display_name: str = "Owner",
    provider: str = "github",
) -> dict[str, str]:
    """Mint user + verified email + oauth_identity + org + Owner
    membership in a single transaction. Idempotent against the same
    ``(email, external_subject, org_slug)``. The provider defaults to
    ``github``; tests using the ``oauth_test`` stub pass ``provider="test"``
    so the subsequent test-stub login matches by identity.

    Deliberate side-effect: calls ``orgs.create_org`` and
    ``orgs.create_membership`` so this seed path emits ``org.created`` and
    ``membership.created`` audit rows — the same as the production
    admin-onboarding path would produce.
    """
    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.core.auth import Role  # noqa: PLC0415
    from app.core.identity import (  # noqa: PLC0415
        add_email,
        create_user,
        link_oauth_identity,
    )
    from app.domain.orgs import (  # noqa: PLC0415
        create_membership,
        create_org,
    )

    async with db_session() as s:
        user = await create_user(s, display_name=display_name)
        await add_email(
            s,
            user_id=user.id,
            email=email.lower(),
            is_primary=True,
            verified=True,
        )
        await link_oauth_identity(
            s,
            user_id=user.id,
            provider=provider,
            external_subject=str(github_id),
            verified=True,
        )
        org = await create_org(s, slug=org_slug, display_name=org_slug, actor=Actor.system())
        await create_membership(
            s,
            user_id=user.id,
            org_id=org.id,
            role=Role.OWNER,
            handle=email.split("@", 1)[0][:64].lower(),
            actor=Actor.system(),
        )
        # Non-prod: seed IAM ARN + region from env so test-stack agents can
        # exchange identity against mock-aws without manual UI configuration.
        import os as _os  # noqa: PLC0415

        from app.core.tenancy import update_org_fields  # noqa: PLC0415

        seed_arn = _os.environ.get("YAAOS_DEV_SEED_ARN")
        seed_region = _os.environ.get("YAAOS_DEV_SEED_REGION")
        if seed_arn and seed_region:
            await update_org_fields(s, org.id, registered_iam_arn=seed_arn, aws_region=seed_region)
        await s.commit()
        return {"user_id": str(user.id), "org_id": str(org.id), "org_slug": org_slug}


async def seed_user_with_session(*, email: str, raw_session_token: str) -> str:
    """Bind ``raw_session_token`` to the user identified by ``email``. Creates
    the user + verified primary email if missing. Caller sets the
    ``yaaos_session`` cookie to ``raw_session_token`` and the backend resolves
    the session normally."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from app.core.identity import (  # noqa: PLC0415
        add_email,
        create_user,
        find_user_by_email,
        hash_token,
        set_session_for_tests,
    )

    async with db_session() as s:
        existing = await find_user_by_email(s, email)
        if existing is not None:
            user = existing
        else:
            user = await create_user(s, display_name=email.split("@", 1)[0])
            await add_email(
                s,
                user_id=user.id,
                email=email.lower(),
                is_primary=True,
                verified=True,
            )
        await set_session_for_tests(
            s,
            token_hash=hash_token(raw_session_token),
            user_id=user.id,
            workspace_id=None,
            csrf_token="e2e-csrf",
            ip=None,
            user_agent="e2e",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await s.commit()
        return str(user.id)


def stage_oauth_test_profile(
    *, external_subject: str, primary_email: str, email_verified: bool, display_name: str
) -> None:
    """Stash the next profile the ``oauth_test`` provider will return."""
    from app.core.identity import ProviderProfile  # noqa: PLC0415

    # `plugins.oauth_test` loads only under APP_MODE=test; this helper is
    # imported by code that runs in dev too, so import lazily.
    from app.plugins.oauth_test import set_next_profile  # noqa: PLC0415

    set_next_profile(
        ProviderProfile(
            external_subject=external_subject,
            primary_email=primary_email,
            email_verified=email_verified,
            display_name=display_name,
        )
    )


# ── agent / workspace seed helpers ──────────────────────────────────


async def seed_org(*, slug: str | None = None, display_name: str = "") -> UUID:
    """Insert a minimal org row via ``domain/orgs.insert_org`` and return its id.

    Sugar over the production ``insert_org`` for tests that previously
    synthesized ``org_id = uuid4()`` without persisting a matching ``orgs``
    row.  Use this to replace those ``uuid4()`` placeholders so production
    paths (`_build_config_update_dto`, audit, SSE) that read from ``orgs``
    don't fail with ``LookupError``.

    Opens its own session and commits.  Returns the new ``org_id``.
    """
    from app.domain.orgs import insert_org  # noqa: PLC0415

    _slug = slug or f"test-org-{uuid7().hex[:12]}"
    async with db_session() as s:
        org = await insert_org(s, slug=_slug, display_name=display_name or _slug)
        await s.commit()
    return org.org_id


async def seed_agent(
    *,
    org_id: UUID,
    iam_arn: str = "arn:aws:iam::123456789012:role/yaaos-agent",
    version: str = "0.0.1",
    instance_id: str | None = None,
) -> dict:
    """Insert a workspace-agent row via public service APIs.

    Opens its own session and commits. Returns ``{"id": ..., "instance_id":
    ..., "org_id": ...}``. Callers that need a backdated ``last_heartbeat_at``
    update the row manually after this call using their own session.

    The caller is responsible for ensuring an ``orgs`` row exists for
    ``org_id``.  Production code (`_build_config_update_dto`, audit row writes,
    SSE) reads from ``orgs``, so a missing org row surfaces as a `LookupError`.
    """
    from app.core.agent_gateway import ensure_agent_row  # noqa: PLC0415

    _instance_id = instance_id or f"test-instance-{uuid4().hex[:8]}"
    async with db_session() as s:
        agent_id = await ensure_agent_row(
            org_id=org_id,
            instance_id=_instance_id,
            iam_arn=iam_arn,
            version=version,
            session=s,
        )
        await s.commit()
    return {"id": agent_id, "instance_id": _instance_id, "org_id": org_id}


async def seed_workspace(
    *,
    org_id: UUID,
    provider_id: str,
    sha: str,
    agent_id: UUID,
    current_command_id: UUID | None = None,
    status: str | None = None,
) -> str:
    """Insert a workspace row for test purposes.

    Opens its own session and commits. Returns the workspace id string.
    ``agent_id`` (required) sets the owning_agent_id column.
    ``current_command_id`` is optional — set it when the test needs to simulate
    a claimed workspace.

    Uses raw SQL to avoid importing WorkspaceRow across module boundaries.
    The workspaces.id column uses uuidv7() server-default (v7 enforced by DB
    check-constraint).
    """
    import json as _json  # noqa: PLC0415
    from datetime import UTC, timedelta  # noqa: PLC0415

    from sqlalchemy import text  # noqa: PLC0415

    _status = status or "active"
    _expires_at = datetime.now(UTC) + timedelta(hours=1)
    _spec = _json.dumps({"sha": sha})

    async with db_session() as s:
        result = await s.execute(
            text(
                "INSERT INTO workspaces"
                " (org_id, owning_agent_id, provider_id, spec, status, expires_at, current_command_id,"
                " destroy_attempts)"
                " VALUES (:org_id, :agent_id, :provider_id, cast(:spec as jsonb), :status, :expires_at,"
                " :cmd_id, 0)"
                " RETURNING id"
            ),
            {
                "org_id": org_id,
                "agent_id": agent_id,
                "provider_id": provider_id,
                "spec": _spec,
                "status": _status,
                "expires_at": _expires_at,
                "cmd_id": current_command_id,
            },
        )
        ws_id = result.scalar_one()
        await s.commit()
    return str(ws_id)


async def delete_org(org_id: UUID) -> None:
    """Hard-delete an org row. Opens its own session and commits. Cascades at
    the DB level to memberships, invitations, and all child rows."""
    from app.core.tenancy import delete_org as _tenancy_delete_org  # noqa: PLC0415

    async with db_session() as s:
        await _tenancy_delete_org(s, org_id)
        await s.commit()


async def delete_user(user_id: UUID) -> None:
    """Hard-delete a user row. Opens its own session and commits. Cascades at
    the DB level to emails, OAuth identities, and sessions — callers that
    need cross-module cleanup (e.g. memberships) must call those separately
    before this."""
    from app.core.identity import delete_user as _identity_delete_user  # noqa: PLC0415

    async with db_session() as s:
        await _identity_delete_user(s, user_id=user_id)
        await s.commit()


async def set_session_last_seen(
    db: object,
    *,
    token_hash: str,
    last_seen_at: datetime,
) -> None:
    """Write ``last_seen_at`` for a session row identified by ``token_hash``.
    Uses the caller's session; does not commit."""
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: PLC0415

    from app.core.identity import set_session_last_seen_for_tests  # noqa: PLC0415

    if not isinstance(db, AsyncSession):
        raise TypeError("set_session_last_seen: first arg must be AsyncSession")
    await set_session_last_seen_for_tests(db, token_hash=token_hash, last_seen_at=last_seen_at)


async def seed_member_for_org(
    *,
    org_slug: str,
    email: str,
    github_id: str,
    role: str = "builder",
    display_name: str = "Member",
    provider: str = "github",
) -> dict[str, str]:
    """Create a user + verified email + OAuth identity + org membership on an
    existing org. The org must already exist (seeded via bootstrap_owner).

    Returns ``{"user_id": ..., "org_id": ..., "org_slug": ...}``.
    """
    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.core.auth import Role  # noqa: PLC0415
    from app.core.identity import (  # noqa: PLC0415
        add_email,
        create_user,
        link_oauth_identity,
    )
    from app.domain.orgs import create_membership, get_org_by_slug  # noqa: PLC0415

    org = await get_org_by_slug(org_slug)
    if org is None:
        raise ValueError(f"org {org_slug!r} not found — seed it first via bootstrap_owner")

    async with db_session() as s:
        user = await create_user(s, display_name=display_name)
        await add_email(
            s,
            user_id=user.id,
            email=email.lower(),
            is_primary=True,
            verified=True,
        )
        await link_oauth_identity(
            s,
            user_id=user.id,
            provider=provider,
            external_subject=str(github_id),
            verified=True,
        )
        await create_membership(
            s,
            user_id=user.id,
            org_id=org.id,
            role=Role(role),
            handle=email.split("@", 1)[0][:64].lower(),
            actor=Actor.system(),
        )
        await s.commit()
        return {"user_id": str(user.id), "org_id": str(org.id), "org_slug": org_slug}


async def seed_workspace_agent(*, org_slug: str, lifecycle: str | None = None) -> dict[str, str]:
    """Seed a reachable ``workspace_agents`` row for the given org slug.

    Inserts the row with ``state="reachable"`` and a recent ``last_heartbeat_at``
    so the Workspaces page's 1-hour retention window includes it immediately.
    Publishes ``agent_changed`` after commit so the page's pure-SSE path
    invalidates the agents query without a manual reload. Returns the agent's
    ``id`` and ``instance_id``.

    The optional ``lifecycle`` kwarg overrides the default ``"unconfigured"``
    state. Accepted values: ``"unconfigured"``, ``"active"``, ``"draining"``,
    ``"shutdown"``.
    """
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    from app.core.sse import GeneralEventKind, publish_general_after_commit  # noqa: PLC0415
    from app.domain.orgs import get_org_by_slug  # noqa: PLC0415

    org = await get_org_by_slug(org_slug)
    if org is None:
        raise ValueError(f"org {org_slug!r} not found — seed it first via bootstrap_owner")
    result = await seed_agent(org_id=org.id)

    if lifecycle and lifecycle != "unconfigured":
        # Override lifecycle via raw SQL — acceptable in test-only seed path.
        async with db_session() as s:
            await s.execute(
                sa_text("UPDATE workspace_agents SET lifecycle = :lc WHERE id = :id"),
                {"lc": lifecycle, "id": result["id"]},
            )
            await s.commit()

    async with db_session() as s:
        publish_general_after_commit(
            s,
            org_id=org.id,
            kind=GeneralEventKind.AGENT_CHANGED,
            payload={"agent_id": str(result["id"])},
        )
        await s.commit()
    return {"id": str(result["id"]), "instance_id": result["instance_id"]}


async def deregister_workspace_agent(*, agent_id: UUID) -> dict[str, str]:
    """Simulate an agent's graceful-shutdown "going away" signal.

    Mirrors ``DELETE /api/v1/agent/identity`` for the agent with the given
    canonical ``id``: marks the row offline + ``last_shutdown_at``, runs
    agent-loss cleanup, and publishes ``agent_changed`` so the dashboard
    flips the card offline live. Drives the graceful-shutdown Playwright spec
    without a real bearer or running container.
    """
    from app.core.agent_gateway import (  # noqa: PLC0415
        get_agent_info,
        get_report_sink,
        mark_agent_offline,
    )
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415
    from app.core.sse import GeneralEventKind, publish_general_after_commit  # noqa: PLC0415

    async with db_session() as s:
        info = await get_agent_info(agent_id, session=s)
    if info is None:
        raise ValueError(f"workspace agent id={agent_id!r} not found")
    org_id = info["org_id"]

    async with org_context(org_id, ActorKind.WORKSPACE, actor_id=agent_id):
        async with db_session() as s:
            await mark_agent_offline(agent_id, session=s)
            await get_report_sink().handle_agent_loss({agent_id}, s)
            publish_general_after_commit(
                s,
                org_id=org_id,
                kind=GeneralEventKind.AGENT_CHANGED,
                payload={"agent_id": str(agent_id)},
            )
            await s.commit()
    return {"id": str(agent_id), "instance_id": info["instance_id"]}


def read_and_clear_email_inbox() -> list[dict[str, str]]:
    """Return + clear the in-memory inbox ``domain.orgs.email.send_plain`` writes
    to in test env."""
    from app.domain.orgs import read_sent_emails  # noqa: PLC0415

    inbox = read_sent_emails()
    out = [{"to": m.to, "subject": m.subject, "body": m.body} for m in inbox]
    inbox.clear()
    return out


__all__ = [
    "DEFAULT_ORG_ID",
    "delete_org",
    "delete_user",
    "deregister_workspace_agent",
    "is_dev_env",
    "read_and_clear_email_inbox",
    "reset",
    "seed_agent",
    "seed_bootstrap_owner",
    "seed_broken_integration",
    "seed_github_install",
    "seed_lesson",
    "seed_member_for_org",
    "seed_paused_run",
    "seed_pipeline",
    "seed_repo_skill",
    "seed_running_run",
    "seed_trigger_binding",
    "seed_user_with_session",
    "seed_workspace",
    "seed_workspace_agent",
    "set_session_last_seen",
    "stage_oauth_test_profile",
]
