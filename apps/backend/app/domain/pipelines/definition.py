"""Pipeline definition model — the authored per-org content edited by the
Pipelines page — plus the flatten/validate logic over it.

Storage never flattens: `PipelineDefinition.stages` keeps `PipelineCallStage`
entries as-is, so a callee's edits reach every caller's *future* runs. The
real flatten (against current definitions, pinned per-run) is the run
engine's job. `flatten()` here recursively expands call stages against a
supplied org-wide definition map; `validate_definition()` dry-runs that
flatten for the edited pipeline AND every pipeline that transitively calls
it, so `create_pipeline`/`update_pipeline` can reject a cycle or a duplicate
flattened stage name before anything is saved. The dry-run result is
discarded — only the tables' rows are ever the persisted shape.

`id` fields default to a fresh uuid7 (`Field(default_factory=uuid7)`) so the
Pipelines editor can omit `id` on a brand-new pipeline or stage and the
server mints one at parse time; round-tripping a stored definition (whose
JSON already carries every id) is unaffected — the default only fires when
the key is absent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal
from uuid import UUID, uuid7

from pydantic import BaseModel, Field

_STAGE_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"


class BoundaryControl(BaseModel, frozen=True):
    """Flat per-stage "what to do next" setting; `on_*` evaluated only when
    `mode == "conditional"`."""

    mode: Literal["always_hitl", "always_proceed", "conditional"] = "always_hitl"
    on_blocker_residuals: bool = False
    on_should_fix_residuals: bool = False
    on_protected_code: bool = False
    on_confidence_below: Literal["medium", "high"] | None = None


class ReviewConfig(BaseModel, frozen=True):
    """Review skill name + max iterations; `None` on the owning stage means
    review is off. Finding display prefixes come from the review skill's
    per-finding `category`, not from config."""

    skill_name: str
    max_iterations: int = Field(ge=1, le=3)


class SkillStage(BaseModel, frozen=True):
    """A main-skill invocation stage; optionally carries a review loop."""

    kind: Literal["skill"] = "skill"
    id: UUID = Field(default_factory=uuid7)
    name: str = Field(pattern=_STAGE_NAME_PATTERN)
    description: str = ""
    skill_name: str
    coding_agent_plugin_id: str
    model: str
    effort: str
    review: ReviewConfig | None = None
    context_stages: tuple[str, ...] | None = None
    wallclock_seconds: int = 3600
    boundary: BoundaryControl


class ReviewSkillStage(BaseModel, frozen=True):
    """A stage whose main invocation speaks the review contract directly —
    produces findings, no artifact, structurally cannot carry a review loop."""

    kind: Literal["review"] = "review"
    id: UUID = Field(default_factory=uuid7)
    name: str = Field(pattern=_STAGE_NAME_PATTERN)
    description: str = ""
    skill_name: str
    coding_agent_plugin_id: str
    model: str
    effort: str
    context_stages: tuple[str, ...] | None = None
    wallclock_seconds: int = 3600
    boundary: BoundaryControl


class ActionStage(BaseModel, frozen=True):
    """A synchronous control-plane action stage. No `name` — actions carry
    no artifact identity, send-back key, or re-run inheritance key."""

    kind: Literal["action"] = "action"
    id: UUID = Field(default_factory=uuid7)
    description: str = ""
    action_id: str


class PipelineCallStage(BaseModel, frozen=True):
    """Calls another org pipeline; expands recursively at flatten time."""

    kind: Literal["call"] = "call"
    id: UUID = Field(default_factory=uuid7)
    description: str = ""
    pipeline_id: UUID


Stage = Annotated[
    SkillStage | ReviewSkillStage | ActionStage | PipelineCallStage, Field(discriminator="kind")
]

# The kinds that survive a flatten — `PipelineCallStage` expands away.
ExecutableStage = SkillStage | ReviewSkillStage | ActionStage


class PipelineDefinition(BaseModel, frozen=True):
    """The authored content — what POST/PUT accept. Shipped defaults are code
    instances with pinned uuid7 ids."""

    id: UUID = Field(default_factory=uuid7)
    name: str
    description: str = ""
    stages: tuple[Stage, ...] = Field(min_length=1)


class FlattenedDefinition(BaseModel, frozen=True):
    """The call-expanded projection. Validation-only outside the run engine
    (the CRUD dry-run flatten discards its result); the run engine pins one
    per run as `pipeline_runs.definition_snapshot` and reloads it via
    `from_snapshot` on every task-body dispatch."""

    stages: tuple[ExecutableStage, ...]

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> FlattenedDefinition:
        """Reconstruct from a `pipeline_runs.definition_snapshot` JSONB dict.
        Dispatches each stage dict by its `kind` field explicitly rather than
        relying on `ExecutableStage`'s implicit (non-discriminated) union
        resolution — `stages` isn't `Annotated[..., Field(discriminator=...)]`,
        so this is the robust parse path for untyped JSON, independent of
        Pydantic's smart-union heuristic."""
        stages = tuple(_STAGE_KIND_TO_MODEL[raw["kind"]].model_validate(raw) for raw in snapshot["stages"])
        return cls(stages=stages)


_STAGE_KIND_TO_MODEL: dict[str, type[SkillStage] | type[ReviewSkillStage] | type[ActionStage]] = {
    "skill": SkillStage,
    "review": ReviewSkillStage,
    "action": ActionStage,
}


class PipelineValidationError(ValueError):
    """Flatten-time collision or cycle across a pipeline + its transitive callers."""


def flatten(
    definition: PipelineDefinition, *, org_definitions: Mapping[UUID, PipelineDefinition]
) -> FlattenedDefinition:
    """Recursively expand `PipelineCallStage` entries against
    `org_definitions` (the org's full id -> definition map, with `definition`
    itself already substituted in by the caller when validating an edit).

    Raises `PipelineValidationError` on a call cycle, an unresolvable call
    target, or a duplicate flattened stage name (name-bearing stages only —
    skill + review stages; action/call stages carry no name).
    """
    stages: list[ExecutableStage] = []
    seen_names: set[str] = set()

    def _expand(defn: PipelineDefinition, chain: tuple[UUID, ...]) -> None:
        if defn.id in chain:
            cycle = " -> ".join(str(c) for c in (*chain, defn.id))
            raise PipelineValidationError(f"pipeline call cycle: {cycle}")
        next_chain = (*chain, defn.id)
        for stage in defn.stages:
            if isinstance(stage, PipelineCallStage):
                callee = org_definitions.get(stage.pipeline_id)
                if callee is None:
                    raise PipelineValidationError(
                        f"call stage {stage.id} references unknown pipeline {stage.pipeline_id}"
                    )
                _expand(callee, next_chain)
                continue
            if isinstance(stage, ActionStage):
                stages.append(stage)
                continue
            if stage.name in seen_names:
                raise PipelineValidationError(f"duplicate flattened stage name: {stage.name!r}")
            seen_names.add(stage.name)
            stages.append(stage)

    _expand(definition, ())
    return FlattenedDefinition(stages=tuple(stages))


def _transitive_callers(pipeline_id: UUID, definitions: Mapping[UUID, PipelineDefinition]) -> set[UUID]:
    """All pipeline ids whose definition calls `pipeline_id`, directly or
    transitively through other calls."""
    direct_callers: dict[UUID, set[UUID]] = {}
    for pid, defn in definitions.items():
        for stage in defn.stages:
            if isinstance(stage, PipelineCallStage):
                direct_callers.setdefault(stage.pipeline_id, set()).add(pid)

    callers: set[UUID] = set()
    frontier = {pipeline_id}
    while frontier:
        next_frontier: set[UUID] = set()
        for target in frontier:
            for caller in direct_callers.get(target, ()):
                if caller not in callers:
                    callers.add(caller)
                    next_frontier.add(caller)
        frontier = next_frontier
    return callers


def validate_definition(
    definition: PipelineDefinition, *, org_definitions: Mapping[UUID, PipelineDefinition]
) -> None:
    """Dry-run flatten of `definition` plus every pipeline in
    `org_definitions` that calls it, directly or transitively — substituting
    `definition` in for its own id first, so editing a callee revalidates
    every caller's future runs. Raises `PipelineValidationError` on a cycle
    or a duplicate flattened stage name anywhere in that set. The flatten
    result is discarded.
    """
    candidates: dict[UUID, PipelineDefinition] = {**org_definitions, definition.id: definition}
    for pipeline_id in (definition.id, *_transitive_callers(definition.id, candidates)):
        flatten(candidates[pipeline_id], org_definitions=candidates)
