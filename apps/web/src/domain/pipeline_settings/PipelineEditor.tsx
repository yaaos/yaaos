/**
 * Pipeline definition editor — name/description fields, the ordered stage
 * list (edit / move / remove per row via a DropdownMenu), the "Add stage"
 * picker, and Save. Used both for editing an existing org pipeline (inside
 * its Accordion row) and for composing a brand-new one (`NewPipelineCard`).
 */

import { apiErrorCode } from "@core/api/public/client";
import {
  type ActionInfoView,
  type PipelineSummaryView,
  useCreatePipeline,
  useDeletePipeline,
  usePipelineDetail,
  useUpdatePipeline,
} from "@core/api/public/queries";
import { ConfirmModal } from "@shared/components/public/layout/confirm-modal";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@shared/components/ui/dropdown-menu";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import { Skeleton } from "@shared/components/ui/skeleton";
import { Textarea } from "@shared/components/ui/textarea";
import {
  ArrowDown,
  ArrowUp,
  GitBranch,
  ListChecks,
  MoreVertical,
  Play,
  Plus,
  Wrench,
} from "lucide-react";
import { useEffect, useState } from "react";
import { StageEditorSheet } from "./StageEditorSheet";
import type { CodingAgentInstall } from "./queries";
import {
  type PipelineDraft,
  type StageDraft,
  detailToDraft,
  draftToWire,
  emptyPipelineDraft,
  newStageDraft,
  pipelineDraftIsValid,
  upstreamStageNames,
} from "./types";

const KIND_ICON: Record<StageDraft["kind"], typeof Play> = {
  skill: Play,
  review: ListChecks,
  action: Wrench,
  call: GitBranch,
};

export interface PicklistData {
  agents: CodingAgentInstall[];
  models: string[];
  efforts: string[];
  actions: ActionInfoView[];
}

function stageSummary(
  stage: StageDraft,
  pipelineOptions: { id: string; name: string }[],
  actions: ActionInfoView[],
): string {
  if (stage.kind === "skill" || stage.kind === "review") return stage.name || "(unnamed)";
  if (stage.kind === "action") {
    const label = actions.find((a) => a.action_id === stage.action_id)?.label;
    return label ?? (stage.action_id || "(no action chosen)");
  }
  return pipelineOptions.find((p) => p.id === stage.pipeline_id)?.name ?? "(no pipeline chosen)";
}

/**
 * The shared stage-list body: name/description fields, ordered stage rows,
 * "Add stage" picker, and the per-stage editor Sheet. Owns no persistence —
 * `draft`/`setDraft` is fully controlled by the caller (`ExistingPipelineEditor`
 * or `NewPipelineCard`), which owns Save/Delete.
 */
function PipelineDraftFields({
  draft,
  setDraft,
  pipelineOptions,
  picklists,
}: {
  draft: PipelineDraft;
  setDraft: (d: PipelineDraft) => void;
  pipelineOptions: { id: string; name: string }[];
  picklists: PicklistData;
}) {
  const [editingKey, setEditingKey] = useState<string | null>(null);
  // Sticky last-edited stage: `StageEditorSheet` must stay mounted across
  // open/close — a Radix Dialog/Sheet needs to remain in the tree to run its
  // own close-transition cleanup (it temporarily locks page pointer-events
  // while open; hard-unmounting it via a truthy-conditional wrapper, as
  // opposed to toggling its `open` prop, can strand that lock and leave the
  // whole page unclickable). So `lastEditingStage` remembers the most
  // recent match and the Sheet mounts once and stays mounted; `open` is the
  // only thing that ever toggles.
  const [lastEditingStage, setLastEditingStage] = useState<StageDraft | null>(null);
  const editingStage = draft.stages.find((s) => s.key === editingKey) ?? null;
  useEffect(() => {
    if (editingStage) setLastEditingStage(editingStage);
  }, [editingStage]);
  const editingIndex = editingStage
    ? draft.stages.indexOf(editingStage)
    : lastEditingStage
      ? draft.stages.findIndex((s) => s.key === lastEditingStage.key)
      : -1;

  function updateStage(next: StageDraft) {
    setDraft({
      ...draft,
      stages: draft.stages.map((s) => (s.key === next.key ? next : s)),
    });
  }

  function addStage(kind: StageDraft["kind"]) {
    const stage = newStageDraft(kind);
    setDraft({ ...draft, stages: [...draft.stages, stage] });
    setEditingKey(stage.key);
  }

  function removeStage(key: string) {
    setDraft({ ...draft, stages: draft.stages.filter((s) => s.key !== key) });
  }

  function moveStage(key: string, direction: -1 | 1) {
    const idx = draft.stages.findIndex((s) => s.key === key);
    const target = idx + direction;
    if (idx === -1 || target < 0 || target >= draft.stages.length) return;
    const next = [...draft.stages];
    const [moved] = next.splice(idx, 1);
    if (!moved) return;
    next.splice(target, 0, moved);
    setDraft({ ...draft, stages: next });
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`pipeline-name-${draft.id ?? "new"}`}>Name</Label>
        <Input
          id={`pipeline-name-${draft.id ?? "new"}`}
          data-testid="pipeline-name"
          value={draft.name}
          onChange={(e) => setDraft({ ...draft, name: e.target.value })}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`pipeline-description-${draft.id ?? "new"}`}>Description</Label>
        <Textarea
          id={`pipeline-description-${draft.id ?? "new"}`}
          data-testid="pipeline-description"
          value={draft.description}
          onChange={(e) => setDraft({ ...draft, description: e.target.value })}
        />
      </div>

      <div className="flex flex-col gap-1.5">
        {draft.stages.length === 0 && (
          <p className="text-muted-foreground text-sm" data-testid="pipeline-stages-empty">
            No stages yet — add one below.
          </p>
        )}
        {draft.stages.map((stage, i) => {
          const Icon = KIND_ICON[stage.kind];
          return (
            <div
              key={stage.key}
              data-testid={`pipeline-stage-row-${stage.key}`}
              className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm"
            >
              <Icon className="w-4 h-4 shrink-0 text-muted-foreground" aria-hidden />
              <Badge variant="outline" className="capitalize shrink-0">
                {stage.kind}
              </Badge>
              <span className="flex-1 truncate">
                {stageSummary(stage, pipelineOptions, picklists.actions)}
              </span>
              <Button
                variant="ghost"
                size="sm"
                data-testid={`pipeline-stage-edit-${stage.key}`}
                onClick={() => setEditingKey(stage.key)}
              >
                Edit
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="sm"
                    data-testid={`pipeline-stage-menu-${stage.key}`}
                    aria-label="Stage actions"
                  >
                    <MoreVertical className="w-4 h-4" aria-hidden />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem
                    disabled={i === 0}
                    data-testid={`pipeline-stage-move-up-${stage.key}`}
                    onClick={() => moveStage(stage.key, -1)}
                  >
                    <ArrowUp className="w-4 h-4" aria-hidden />
                    Move up
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    disabled={i === draft.stages.length - 1}
                    data-testid={`pipeline-stage-move-down-${stage.key}`}
                    onClick={() => moveStage(stage.key, 1)}
                  >
                    <ArrowDown className="w-4 h-4" aria-hidden />
                    Move down
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    data-testid={`pipeline-stage-remove-${stage.key}`}
                    onClick={() => removeStage(stage.key)}
                  >
                    Remove
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          );
        })}
      </div>

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="outline"
            size="sm"
            data-testid="pipeline-add-stage"
            className="self-start"
          >
            <Plus className="w-4 h-4" aria-hidden />
            Add stage
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          // Picking a kind opens the stage Sheet in the same tick the menu
          // closes — two Radix focus-scopes competing for focus at once
          // recurses infinitely in jsdom's focus dispatch (real browsers
          // don't hit this). Skipping the menu's close-autofocus lets the
          // Sheet's own autofocus win without a fight.
          onCloseAutoFocus={(e) => e.preventDefault()}
        >
          <DropdownMenuItem
            data-testid="pipeline-add-stage-skill"
            onClick={() => addStage("skill")}
          >
            <Play className="w-4 h-4" aria-hidden />
            Skill
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="pipeline-add-stage-review"
            onClick={() => addStage("review")}
          >
            <ListChecks className="w-4 h-4" aria-hidden />
            Review
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="pipeline-add-stage-action"
            onClick={() => addStage("action")}
          >
            <Wrench className="w-4 h-4" aria-hidden />
            Action
          </DropdownMenuItem>
          <DropdownMenuItem data-testid="pipeline-add-stage-call" onClick={() => addStage("call")}>
            <GitBranch className="w-4 h-4" aria-hidden />
            Call another pipeline
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {lastEditingStage && (
        <StageEditorSheet
          open={editingStage != null}
          onOpenChange={(open) => !open && setEditingKey(null)}
          stage={editingStage ?? lastEditingStage}
          upstreamNames={upstreamStageNames(draft.stages, editingIndex)}
          pipelineOptions={pipelineOptions}
          agents={picklists.agents}
          models={picklists.models}
          efforts={picklists.efforts}
          actions={picklists.actions}
          onSave={updateStage}
        />
      )}
    </div>
  );
}

function saveErrorMessage(err: unknown): string {
  const code = apiErrorCode(err);
  if (code === "invalid_definition") {
    return "Invalid pipeline definition — check for a duplicate stage name or a call cycle.";
  }
  if (code === "name_taken") return "A pipeline with this name already exists.";
  return "Couldn't save this pipeline.";
}

/** Editor body for an existing org pipeline — mounted lazily (only while
 *  its Accordion row is expanded), so the detail fetch is on-demand. */
export function ExistingPipelineEditor({
  pipelineId,
  allPipelines,
  picklists,
}: {
  pipelineId: string;
  allPipelines: PipelineSummaryView[];
  picklists: PicklistData;
}) {
  const { data: detail, isLoading, isError } = usePipelineDetail(pipelineId, { enabled: true });
  const update = useUpdatePipeline();
  const del = useDeletePipeline();
  const [draft, setDraft] = useState<PipelineDraft | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Seed the local draft from the fetched definition exactly once — never
  // recompute inline from `detail` on every render. `detailToDraft` mints a
  // fresh client-only `key` per stage, so recomputing on each render (e.g.
  // triggered by an unrelated query refetch) would silently swap every
  // stage's key out from under the open `StageEditorSheet`, unmounting and
  // remounting it mid-interaction.
  useEffect(() => {
    if (detail) setDraft((prev) => prev ?? detailToDraft(detail));
  }, [detail]);

  const active = draft;

  if (isLoading || !active) {
    return (
      <div className="flex flex-col gap-2 py-2">
        <Skeleton className="h-8" />
        <Skeleton className="h-24" />
      </div>
    );
  }
  if (isError) {
    return <ErrorBanner message="Couldn't load this pipeline." />;
  }

  const pipelineOptions = allPipelines
    .filter((p) => p.id !== pipelineId)
    .map((p) => ({ id: p.id, name: p.name }));

  return (
    <div className="flex flex-col gap-3 py-2">
      <PipelineDraftFields
        draft={active}
        setDraft={setDraft}
        pipelineOptions={pipelineOptions}
        picklists={picklists}
      />
      <div className="flex items-center gap-2">
        <Button
          data-testid="pipeline-save"
          disabled={!pipelineDraftIsValid(active) || update.isPending}
          onClick={() =>
            update.mutate(
              { id: pipelineId, definition: draftToWire(active) },
              { onSuccess: () => setDraft(null) },
            )
          }
        >
          {update.isPending ? "Saving…" : "Save"}
        </Button>
        <Button
          variant="destructive"
          data-testid="pipeline-delete"
          onClick={() => {
            setDeleteError(null);
            setConfirmingDelete(true);
          }}
        >
          Delete
        </Button>
      </div>
      {update.isError && <ErrorBanner message={saveErrorMessage(update.error)} />}
      {deleteError && <ErrorBanner message={deleteError} />}

      <ConfirmModal
        open={confirmingDelete}
        onOpenChange={setConfirmingDelete}
        title={`Delete ${active.name}?`}
        body="This can't be undone."
        confirmLabel="Delete"
        tone="destructive"
        pending={del.isPending}
        onConfirm={() =>
          del.mutate(pipelineId, {
            onSuccess: () => setConfirmingDelete(false),
            onError: (err) => {
              setConfirmingDelete(false);
              const code = apiErrorCode(err);
              setDeleteError(
                code === "referenced"
                  ? "In use by a repo trigger or another pipeline."
                  : "Couldn't delete this pipeline.",
              );
            },
          })
        }
      />
    </div>
  );
}

/** "New pipeline" composer — an inline card above the Accordion, not an
 *  Accordion row (the pipeline doesn't exist server-side until Save). */
export function NewPipelineCard({
  allPipelines,
  picklists,
  onCancel,
  onCreated,
}: {
  allPipelines: PipelineSummaryView[];
  picklists: PicklistData;
  onCancel: () => void;
  onCreated: () => void;
}) {
  const [draft, setDraft] = useState<PipelineDraft>(emptyPipelineDraft());
  const create = useCreatePipeline();

  const pipelineOptions = allPipelines.map((p) => ({ id: p.id, name: p.name }));

  return (
    <section
      className="rounded-lg border border-border bg-card px-4 py-4"
      data-testid="pipeline-new-card"
    >
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold">New pipeline</h3>
        <Button variant="ghost" size="sm" data-testid="pipeline-new-cancel" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      <PipelineDraftFields
        draft={draft}
        setDraft={setDraft}
        pipelineOptions={pipelineOptions}
        picklists={picklists}
      />
      <div className="flex items-center gap-2 mt-3">
        <Button
          data-testid="pipeline-new-save"
          disabled={!pipelineDraftIsValid(draft) || create.isPending}
          onClick={() =>
            create.mutate(draftToWire(draft), {
              onSuccess: onCreated,
            })
          }
        >
          {create.isPending ? "Saving…" : "Create pipeline"}
        </Button>
      </div>
      {create.isError && <ErrorBanner message={saveErrorMessage(create.error)} />}
    </section>
  );
}
