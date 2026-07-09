/**
 * Editable draft shapes for the Pipelines settings page's stage editor.
 *
 * The wire `PipelineDefinition`/`Stage` union (see `core/api/public/queries`)
 * is what the backend accepts/returns. The editor works over a slightly
 * richer local "draft" shape — a stable client-only `key` for React lists
 * and list-rendering, plus a couple of fields flattened for simpler form
 * state (`reviewEnabled` instead of a nullable nested object,
 * `contextAllUpstream` instead of a nullable array). `draftToWire` /
 * `detailToDraft` convert between the two at the editor's boundary.
 */

import type {
  PipelineDefinitionBody,
  PipelineDetailView,
  StageView,
} from "@core/api/public/queries";

export type BoundaryDraft = {
  mode: "always_hitl" | "always_proceed" | "conditional";
  on_blocker_residuals: boolean;
  on_should_fix_residuals: boolean;
  on_protected_code: boolean;
  on_confidence_below: "medium" | "high" | null;
};

interface StageDraftBase {
  key: string;
  id?: string;
  description: string;
}

export interface SkillStageDraft extends StageDraftBase {
  kind: "skill";
  name: string;
  skill_name: string;
  coding_agent_plugin_id: string;
  model: string;
  effort: string;
  reviewEnabled: boolean;
  review_skill_name: string;
  review_max_iterations: number;
  contextAllUpstream: boolean;
  context_stages: string[];
  wallclock_seconds: number;
  boundary: BoundaryDraft;
}

export interface ReviewSkillStageDraft extends StageDraftBase {
  kind: "review";
  name: string;
  skill_name: string;
  coding_agent_plugin_id: string;
  model: string;
  effort: string;
  contextAllUpstream: boolean;
  context_stages: string[];
  wallclock_seconds: number;
  boundary: BoundaryDraft;
}

export interface ActionStageDraft extends StageDraftBase {
  kind: "action";
  action_id: string;
}

export interface PipelineCallStageDraft extends StageDraftBase {
  kind: "call";
  pipeline_id: string;
}

export type StageDraft =
  | SkillStageDraft
  | ReviewSkillStageDraft
  | ActionStageDraft
  | PipelineCallStageDraft;

export interface PipelineDraft {
  id?: string;
  name: string;
  description: string;
  stages: StageDraft[];
}

const DEFAULT_WALLCLOCK_SECONDS = 3600;

function newKey(): string {
  return crypto.randomUUID();
}

export function emptyBoundary(): BoundaryDraft {
  return {
    mode: "always_hitl",
    on_blocker_residuals: false,
    on_should_fix_residuals: false,
    on_protected_code: false,
    on_confidence_below: null,
  };
}

export function newStageDraft(kind: StageDraft["kind"]): StageDraft {
  switch (kind) {
    case "skill":
      return {
        key: newKey(),
        kind: "skill",
        description: "",
        name: "",
        skill_name: "",
        coding_agent_plugin_id: "",
        model: "",
        effort: "",
        reviewEnabled: false,
        review_skill_name: "",
        review_max_iterations: 1,
        contextAllUpstream: true,
        context_stages: [],
        wallclock_seconds: DEFAULT_WALLCLOCK_SECONDS,
        boundary: emptyBoundary(),
      };
    case "review":
      return {
        key: newKey(),
        kind: "review",
        description: "",
        name: "",
        skill_name: "",
        coding_agent_plugin_id: "",
        model: "",
        effort: "",
        contextAllUpstream: true,
        context_stages: [],
        wallclock_seconds: DEFAULT_WALLCLOCK_SECONDS,
        boundary: emptyBoundary(),
      };
    case "action":
      return { key: newKey(), kind: "action", description: "", action_id: "" };
    case "call":
      return { key: newKey(), kind: "call", description: "", pipeline_id: "" };
  }
}

export function emptyPipelineDraft(): PipelineDraft {
  return { name: "", description: "", stages: [] };
}

function boundaryToDraft(b: {
  mode: "always_hitl" | "always_proceed" | "conditional";
  on_blocker_residuals: boolean;
  on_should_fix_residuals: boolean;
  on_protected_code: boolean;
  on_confidence_below?: ("medium" | "high") | null;
}): BoundaryDraft {
  return {
    mode: b.mode,
    on_blocker_residuals: b.on_blocker_residuals,
    on_should_fix_residuals: b.on_should_fix_residuals,
    on_protected_code: b.on_protected_code,
    on_confidence_below: b.on_confidence_below ?? null,
  };
}

function stageToDraft(stage: StageView): StageDraft {
  if (stage.kind === "skill") {
    return {
      key: newKey(),
      kind: "skill",
      id: stage.id,
      description: stage.description,
      name: stage.name,
      skill_name: stage.skill_name,
      coding_agent_plugin_id: stage.coding_agent_plugin_id,
      model: stage.model,
      effort: stage.effort,
      reviewEnabled: stage.review != null,
      review_skill_name: stage.review?.skill_name ?? "",
      review_max_iterations: stage.review?.max_iterations ?? 1,
      contextAllUpstream: stage.context_stages == null,
      context_stages: stage.context_stages ?? [],
      wallclock_seconds: stage.wallclock_seconds,
      boundary: boundaryToDraft(stage.boundary),
    };
  }
  if (stage.kind === "review") {
    return {
      key: newKey(),
      kind: "review",
      id: stage.id,
      description: stage.description,
      name: stage.name,
      skill_name: stage.skill_name,
      coding_agent_plugin_id: stage.coding_agent_plugin_id,
      model: stage.model,
      effort: stage.effort,
      contextAllUpstream: stage.context_stages == null,
      context_stages: stage.context_stages ?? [],
      wallclock_seconds: stage.wallclock_seconds,
      boundary: boundaryToDraft(stage.boundary),
    };
  }
  if (stage.kind === "action") {
    return {
      key: newKey(),
      kind: "action",
      id: stage.id,
      description: stage.description,
      action_id: stage.action_id,
    };
  }
  return {
    key: newKey(),
    kind: "call",
    id: stage.id,
    description: stage.description,
    pipeline_id: stage.pipeline_id,
  };
}

export function detailToDraft(detail: PipelineDetailView): PipelineDraft {
  return {
    id: detail.id,
    name: detail.name,
    description: detail.description,
    stages: detail.stages.map(stageToDraft),
  };
}

function stageToWire(stage: StageDraft): PipelineDefinitionBody["stages"][number] {
  const base = stage.id ? { id: stage.id } : {};
  if (stage.kind === "skill") {
    return {
      ...base,
      kind: "skill",
      name: stage.name,
      description: stage.description,
      skill_name: stage.skill_name,
      coding_agent_plugin_id: stage.coding_agent_plugin_id,
      model: stage.model,
      effort: stage.effort,
      review: stage.reviewEnabled
        ? {
            skill_name: stage.review_skill_name,
            max_iterations: stage.review_max_iterations,
          }
        : null,
      context_stages: stage.contextAllUpstream ? null : stage.context_stages,
      wallclock_seconds: stage.wallclock_seconds,
      boundary: stage.boundary,
    };
  }
  if (stage.kind === "review") {
    return {
      ...base,
      kind: "review",
      name: stage.name,
      description: stage.description,
      skill_name: stage.skill_name,
      coding_agent_plugin_id: stage.coding_agent_plugin_id,
      model: stage.model,
      effort: stage.effort,
      context_stages: stage.contextAllUpstream ? null : stage.context_stages,
      wallclock_seconds: stage.wallclock_seconds,
      boundary: stage.boundary,
    };
  }
  if (stage.kind === "action") {
    return { ...base, kind: "action", description: stage.description, action_id: stage.action_id };
  }
  return {
    ...base,
    kind: "call",
    description: stage.description,
    pipeline_id: stage.pipeline_id,
  };
}

export function draftToWire(draft: PipelineDraft): PipelineDefinitionBody {
  return {
    ...(draft.id ? { id: draft.id } : {}),
    name: draft.name,
    description: draft.description,
    stages: draft.stages.map(stageToWire),
  };
}

/** Stage names available to a `context_stages` picker for the stage at
 *  `index` — every earlier `skill`/`review` stage (the only kinds that
 *  carry a `name` + produce an artifact/finding an invocation can read). */
export function upstreamStageNames(stages: StageDraft[], index: number): string[] {
  const names: string[] = [];
  for (let i = 0; i < index; i++) {
    const s = stages[i];
    if (s && (s.kind === "skill" || s.kind === "review") && !names.includes(s.name)) {
      names.push(s.name);
    }
  }
  return names;
}

export const STAGE_NAME_RE = /^[a-z][a-z0-9-]{0,63}$/;

/** Whether a single stage draft has everything required to submit. Does
 *  NOT check cross-stage constraints (name uniqueness, cycles) — the
 *  backend is the source of truth there (`invalid_definition`). */
export function stageIsValid(stage: StageDraft): boolean {
  switch (stage.kind) {
    case "skill":
      return (
        STAGE_NAME_RE.test(stage.name) &&
        stage.skill_name.trim().length > 0 &&
        stage.coding_agent_plugin_id.trim().length > 0 &&
        stage.model.trim().length > 0 &&
        stage.effort.trim().length > 0 &&
        stage.wallclock_seconds > 0 &&
        (!stage.reviewEnabled ||
          (stage.review_skill_name.trim().length > 0 &&
            stage.review_max_iterations >= 1 &&
            stage.review_max_iterations <= 3))
      );
    case "review":
      return (
        STAGE_NAME_RE.test(stage.name) &&
        stage.skill_name.trim().length > 0 &&
        stage.coding_agent_plugin_id.trim().length > 0 &&
        stage.model.trim().length > 0 &&
        stage.effort.trim().length > 0 &&
        stage.wallclock_seconds > 0
      );
    case "action":
      return stage.action_id.trim().length > 0;
    case "call":
      return stage.pipeline_id.trim().length > 0;
  }
}

export function pipelineDraftIsValid(draft: PipelineDraft): boolean {
  return (
    draft.name.trim().length > 0 && draft.stages.length > 0 && draft.stages.every(stageIsValid)
  );
}
