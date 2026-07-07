"""Tests for `domain/pipelines.defaults` — the six code-shipped default
`PipelineDefinition`s — plus `service.instantiate_template`'s rewiring.

Pure unit coverage (no DB): each default dry-run flattens cleanly against
the full roster (proves no cycle, no duplicate flattened stage name, every
`PipelineCallStage` target resolves); pinned ids are stable module
constants, not freshly minted per call; `dev`/`troubleshoot` flatten to the
expected stage roster (composition via `PipelineCallStage` into
`implementation`).

Service coverage (DB via `instantiate_template`): materializing `dev` into
a fresh org copies BOTH `dev` and its `implementation` callee as fresh org
rows with the call stage rewired to the new callee id; materializing
`troubleshoot` into the SAME org afterward REUSES the already-materialized
`implementation` row by name rather than copying a second one; an unknown
`template_id` raises `TemplateNotFoundError`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.audit_log import Actor, ActorKind
from app.core.auth import org_context
from app.core.tenancy import create_org
from app.domain.pipelines import (
    PipelineCallStage,
    TemplateNotFoundError,
    defaults,
    get_pipeline,
    instantiate_template,
    list_pipelines,
    list_templates,
)
from app.domain.pipelines.definition import flatten, validate_definition


def test_all_six_defaults_dry_run_flatten() -> None:
    by_id = {t.id: t for t in defaults.ALL_DEFAULTS}
    for template in defaults.ALL_DEFAULTS:
        validate_definition(template, org_definitions=by_id)


def test_pinned_ids_are_stable_across_calls() -> None:
    first_call = {t.name: t.id for t in list_templates()}
    second_call = {t.name: t.id for t in list_templates()}
    assert first_call == second_call
    # Literal pinned constants, not `Field(default_factory=uuid7)` freshly
    # minted per import — cross-references (`PipelineCallStage.pipeline_id`)
    # depend on these never changing between process restarts.
    assert defaults.DEV.id == defaults.DEV_ID
    assert defaults.TROUBLESHOOT.id == defaults.TROUBLESHOOT_ID
    assert defaults.IMPLEMENTATION.id == defaults.IMPLEMENTATION_ID
    assert defaults.PR_REVIEW.id == defaults.PR_REVIEW_ID
    assert defaults.INCREMENTAL_REVIEW.id == defaults.INCREMENTAL_REVIEW_ID
    assert defaults.COMMENT_RESPONSE.id == defaults.COMMENT_RESPONSE_ID


def test_dev_flattens_to_requirements_architecture_plan_then_implementation() -> None:
    by_id = {t.id: t for t in defaults.ALL_DEFAULTS}
    flattened = flatten(defaults.DEV, org_definitions=by_id)
    assert [getattr(s, "name", s.kind) for s in flattened.stages] == [
        "requirements",
        "architecture",
        "plan",
        "implement",
        "action",
    ]


def test_troubleshoot_flattens_to_diagnose_fix_plan_then_implementation() -> None:
    by_id = {t.id: t for t in defaults.ALL_DEFAULTS}
    flattened = flatten(defaults.TROUBLESHOOT, org_definitions=by_id)
    assert [getattr(s, "name", s.kind) for s in flattened.stages] == [
        "diagnose",
        "fix-plan",
        "implement",
        "action",
    ]


@pytest.mark.service
@pytest.mark.asyncio
async def test_instantiate_template_unknown_id_raises_service(db_session) -> None:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    with pytest.raises(TemplateNotFoundError):
        await instantiate_template(
            org_id=org.org_id, template_id=uuid4(), actor=Actor.system(), session=db_session
        )


@pytest.mark.service
@pytest.mark.asyncio
async def test_instantiate_dev_also_copies_implementation_callee_service(db_session) -> None:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")

    dev_pipeline_id = await instantiate_template(
        org_id=org.org_id, template_id=defaults.DEV_ID, actor=Actor.system(), session=db_session
    )
    await db_session.flush()

    async with org_context(org.org_id, ActorKind.SYSTEM):
        summaries = await list_pipelines(org.org_id, session=db_session)
        dev = await get_pipeline(dev_pipeline_id, session=db_session)
    names = sorted(s.name for s in summaries)
    assert names == ["dev", "implementation"]
    assert dev.definition.id != defaults.DEV_ID  # fresh id, not the pinned template id
    call_stage = next(s for s in dev.definition.stages if isinstance(s, PipelineCallStage))
    implementation_summary = next(s for s in summaries if s.name == "implementation")
    assert call_stage.pipeline_id == implementation_summary.id
    assert implementation_summary.id != defaults.IMPLEMENTATION_ID  # fresh id, not the pinned template id


@pytest.mark.service
@pytest.mark.asyncio
async def test_instantiate_troubleshoot_reuses_existing_implementation_by_name_service(db_session) -> None:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")

    await instantiate_template(
        org_id=org.org_id, template_id=defaults.DEV_ID, actor=Actor.system(), session=db_session
    )
    await db_session.flush()
    async with org_context(org.org_id, ActorKind.SYSTEM):
        first_implementation_id = next(
            s.id for s in await list_pipelines(org.org_id, session=db_session) if s.name == "implementation"
        )

    troubleshoot_pipeline_id = await instantiate_template(
        org_id=org.org_id, template_id=defaults.TROUBLESHOOT_ID, actor=Actor.system(), session=db_session
    )
    await db_session.flush()

    async with org_context(org.org_id, ActorKind.SYSTEM):
        summaries = await list_pipelines(org.org_id, session=db_session)
        troubleshoot = await get_pipeline(troubleshoot_pipeline_id, session=db_session)
    names = sorted(s.name for s in summaries)
    # Still exactly ONE implementation row — reused, not copied a second time.
    assert names == ["dev", "implementation", "troubleshoot"]
    call_stage = next(s for s in troubleshoot.definition.stages if isinstance(s, PipelineCallStage))
    assert call_stage.pipeline_id == first_implementation_id
