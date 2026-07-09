"""Unit tests for `domain/pipelines.definition` — flatten/validate over
in-memory definition maps. No DB; pure value-object transformation."""

from __future__ import annotations

from uuid import UUID, uuid7

import pytest

from app.domain.pipelines.definition import (
    ActionStage,
    BoundaryControl,
    PipelineCallStage,
    PipelineDefinition,
    PipelineValidationError,
    SkillStage,
    flatten,
    validate_definition,
)


def _skill_stage(name: str) -> SkillStage:
    return SkillStage(
        name=name,
        skill_name=f"{name}-skill",
        coding_agent_plugin_id="claude_code",
        model="sonnet",
        effort="medium",
        boundary=BoundaryControl(),
    )


def _call_stage(pipeline_id: UUID) -> PipelineCallStage:
    return PipelineCallStage(pipeline_id=pipeline_id)


def _definition(name: str, stages: tuple, *, id_: UUID | None = None) -> PipelineDefinition:
    kwargs = {"name": name, "stages": stages}
    if id_ is not None:
        kwargs["id"] = id_
    return PipelineDefinition(**kwargs)


def test_stage_and_definition_ids_default_when_omitted() -> None:
    stage = _skill_stage("only-stage")
    definition = _definition("no-id-supplied", (stage,))
    assert isinstance(stage.id, UUID)
    assert isinstance(definition.id, UUID)
    # Two independently-constructed stages never collide.
    assert _skill_stage("only-stage").id != stage.id


def test_flatten_expands_nested_calls_in_place() -> None:
    callee = _definition("callee", (_skill_stage("callee-stage"),))
    caller = _definition("caller", (_skill_stage("first"), _call_stage(callee.id), _skill_stage("last")))

    flattened = flatten(caller, org_definitions={callee.id: callee})

    assert [s.name for s in flattened.stages] == ["first", "callee-stage", "last"]


def test_flatten_keeps_action_stages_with_no_name() -> None:
    action = ActionStage(action_id="notify-slack")
    definition = _definition("with-action", (_skill_stage("first"), action))

    flattened = flatten(definition, org_definitions={})

    assert flattened.stages[1] is action


def test_flatten_detects_call_cycle() -> None:
    id_a, id_b = uuid7(), uuid7()
    def_a = _definition("a", (_call_stage(id_b),), id_=id_a)
    def_b = _definition("b", (_call_stage(id_a),), id_=id_b)

    with pytest.raises(PipelineValidationError):
        flatten(def_a, org_definitions={id_a: def_a, id_b: def_b})


def test_flatten_detects_self_call_cycle() -> None:
    id_a = uuid7()
    def_a = _definition("a", (_call_stage(id_a),), id_=id_a)

    with pytest.raises(PipelineValidationError):
        flatten(def_a, org_definitions={id_a: def_a})


def test_flatten_rejects_unknown_call_target() -> None:
    definition = _definition("caller", (_call_stage(uuid7()),))

    with pytest.raises(PipelineValidationError):
        flatten(definition, org_definitions={})


def test_flatten_detects_duplicate_flattened_stage_name_via_call() -> None:
    callee = _definition("callee", (_skill_stage("shared-name"),))
    caller = _definition("caller", (_skill_stage("shared-name"), _call_stage(callee.id)))

    with pytest.raises(PipelineValidationError):
        flatten(caller, org_definitions={callee.id: callee})


def test_validate_definition_passes_for_non_conflicting_edit() -> None:
    callee = _definition("callee", (_skill_stage("shared"),))
    caller = _definition("caller", (_skill_stage("caller-only"), _call_stage(callee.id)))
    org_definitions = {callee.id: callee, caller.id: caller}

    edited_callee = callee.model_copy(update={"description": "renamed description"})

    # Should not raise.
    validate_definition(edited_callee, org_definitions=org_definitions)


def test_validate_definition_revalidates_transitive_callers() -> None:
    """Editing a callee to introduce a name collision with one of its
    callers is rejected even though only the callee is being validated —
    `validate_definition` dry-runs every transitive caller too."""
    callee = _definition("callee", (_skill_stage("shared"),))
    caller = _definition("caller", (_skill_stage("caller-only"), _call_stage(callee.id)))
    org_definitions = {callee.id: callee, caller.id: caller}

    edited_callee = callee.model_copy(update={"stages": (_skill_stage("caller-only"),)})

    with pytest.raises(PipelineValidationError):
        validate_definition(edited_callee, org_definitions=org_definitions)


def test_validate_definition_revalidates_transitive_caller_two_hops_away() -> None:
    """caller_of_caller -> mid -> callee: editing `callee` must also
    revalidate `caller_of_caller`, not just its direct caller `mid`."""
    callee = _definition("callee", (_skill_stage("callee-stage"),))
    mid = _definition("mid", (_call_stage(callee.id),))
    caller_of_caller = _definition("caller-of-caller", (_skill_stage("callee-stage"), _call_stage(mid.id)))
    org_definitions = {
        callee.id: callee,
        mid.id: mid,
        caller_of_caller.id: caller_of_caller,
    }

    # No-op edit (still keeps the original name) — collides transitively
    # through `mid` with `caller_of_caller`'s own "callee-stage" stage.
    edited_callee = callee.model_copy(update={"description": "still collides"})

    with pytest.raises(PipelineValidationError):
        validate_definition(edited_callee, org_definitions=org_definitions)
