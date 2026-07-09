/**
 * Per-kind stage editor — a right-anchored Sheet. Edits a local copy of the
 * stage draft; "Save" commits it back to the pipeline draft's stage list
 * (`PipelineEditor` owns the array), "Cancel" discards.
 *
 * Picklist data (installed coding agents, model/effort defaults, registered
 * actions) is fetched once at the page level and threaded down as props —
 * keeps every `useSuspenseQuery` resolved before the page's first paint
 * instead of suspending deep inside a conditionally-mounted Sheet.
 */

import type { ActionInfoView } from "@core/api/public/queries";
import { Button } from "@shared/components/ui/button";
import { Checkbox } from "@shared/components/ui/checkbox";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@shared/components/ui/collapsible";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@shared/components/ui/radio-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@shared/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@shared/components/ui/sheet";
import { Switch } from "@shared/components/ui/switch";
import { Textarea } from "@shared/components/ui/textarea";
import { ChevronDown } from "lucide-react";
import { useEffect, useState } from "react";
import type { CodingAgentInstall } from "./queries";
import { STAGE_NAME_RE, type StageDraft, stageIsValid } from "./types";

interface StageEditorSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  stage: StageDraft;
  upstreamNames: string[];
  pipelineOptions: { id: string; name: string }[];
  agents: CodingAgentInstall[];
  models: string[];
  efforts: string[];
  actions: ActionInfoView[];
  onSave: (stage: StageDraft) => void;
}

const KIND_LABEL: Record<StageDraft["kind"], string> = {
  skill: "Skill stage",
  review: "Review stage",
  action: "Action stage",
  call: "Call stage",
};

export function StageEditorSheet({
  open,
  onOpenChange,
  stage,
  upstreamNames,
  pipelineOptions,
  agents,
  models,
  efforts,
  actions,
  onSave,
}: StageEditorSheetProps) {
  const [draft, setDraft] = useState<StageDraft>(stage);

  // Re-seed local state whenever the sheet opens for a (possibly different) stage.
  useEffect(() => {
    if (open) setDraft(stage);
  }, [open, stage]);

  const canSave = stageIsValid(draft);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" data-testid="stage-editor" className="sm:max-w-xl overflow-y-auto">
        <SheetHeader>
          <SheetTitle>{KIND_LABEL[draft.kind]}</SheetTitle>
        </SheetHeader>
        <div className="flex flex-col gap-4 py-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="stage-description">Description</Label>
            <Textarea
              id="stage-description"
              data-testid="stage-description"
              value={draft.description}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
              placeholder="Notes for anyone reading this pipeline"
            />
          </div>

          {(draft.kind === "skill" || draft.kind === "review") && (
            <SkillCommonFields
              draft={draft}
              setDraft={setDraft}
              agents={agents}
              models={models}
              efforts={efforts}
            />
          )}

          {draft.kind === "skill" && <ReviewLoopFields draft={draft} setDraft={setDraft} />}

          {(draft.kind === "skill" || draft.kind === "review") && (
            <>
              <ContextStagesFields
                draft={draft}
                setDraft={setDraft}
                upstreamNames={upstreamNames}
              />

              <Collapsible>
                <CollapsibleTrigger asChild>
                  <button
                    type="button"
                    className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
                    data-testid="stage-advanced-toggle"
                  >
                    <ChevronDown className="w-3.5 h-3.5" aria-hidden />
                    Advanced settings
                  </button>
                </CollapsibleTrigger>
                <CollapsibleContent className="pt-2">
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="stage-wallclock">Wallclock timeout (seconds)</Label>
                    <Input
                      id="stage-wallclock"
                      data-testid="stage-wallclock"
                      type="number"
                      min={1}
                      value={draft.wallclock_seconds}
                      onChange={(e) =>
                        setDraft({ ...draft, wallclock_seconds: e.target.valueAsNumber || 0 })
                      }
                    />
                  </div>
                </CollapsibleContent>
              </Collapsible>

              <BoundaryFields draft={draft} setDraft={setDraft} />
            </>
          )}

          {draft.kind === "action" && (
            <ActionFields draft={draft} setDraft={setDraft} actions={actions} />
          )}

          {draft.kind === "call" && (
            <CallFields draft={draft} setDraft={setDraft} pipelineOptions={pipelineOptions} />
          )}
        </div>
        <SheetFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            data-testid="stage-editor-save"
            disabled={!canSave}
            onClick={() => {
              onSave(draft);
              onOpenChange(false);
            }}
          >
            Save stage
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}

function SkillCommonFields<T extends StageDraft & { kind: "skill" | "review" }>({
  draft,
  setDraft,
  agents,
  models,
  efforts,
}: {
  draft: T;
  setDraft: (d: T) => void;
  agents: CodingAgentInstall[];
  models: string[];
  efforts: string[];
}) {
  const nameValid = draft.name === "" || STAGE_NAME_RE.test(draft.name);

  return (
    <>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="stage-name">Stage name</Label>
        <Input
          id="stage-name"
          data-testid="stage-name"
          value={draft.name}
          onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          placeholder="e.g. requirements"
        />
        {!nameValid && (
          <p className="text-xs text-destructive">
            Lowercase letters, digits, and hyphens only; must start with a letter.
          </p>
        )}
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="stage-skill-name">Skill</Label>
        <Input
          id="stage-skill-name"
          data-testid="stage-skill-name"
          value={draft.skill_name}
          onChange={(e) => setDraft({ ...draft, skill_name: e.target.value })}
          placeholder="e.g. requirements"
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="stage-agent">Coding agent</Label>
        <Select
          value={draft.coding_agent_plugin_id}
          onValueChange={(v) => setDraft({ ...draft, coding_agent_plugin_id: v })}
        >
          <SelectTrigger id="stage-agent" data-testid="stage-agent">
            <SelectValue placeholder="Choose a coding agent…" />
          </SelectTrigger>
          <SelectContent>
            {agents.map((a) => (
              <SelectItem key={a.plugin_id} value={a.plugin_id}>
                {a.plugin_id}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="stage-model">Model</Label>
          <Select value={draft.model} onValueChange={(v) => setDraft({ ...draft, model: v })}>
            <SelectTrigger id="stage-model" data-testid="stage-model">
              <SelectValue placeholder="Model…" />
            </SelectTrigger>
            <SelectContent>
              {models.map((m) => (
                <SelectItem key={m} value={m}>
                  {m}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="stage-effort">Effort</Label>
          <Select value={draft.effort} onValueChange={(v) => setDraft({ ...draft, effort: v })}>
            <SelectTrigger id="stage-effort" data-testid="stage-effort">
              <SelectValue placeholder="Effort…" />
            </SelectTrigger>
            <SelectContent>
              {efforts.map((e) => (
                <SelectItem key={e} value={e}>
                  {e}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
    </>
  );
}

function ReviewLoopFields({
  draft,
  setDraft,
}: {
  draft: Extract<StageDraft, { kind: "skill" }>;
  setDraft: (d: Extract<StageDraft, { kind: "skill" }>) => void;
}) {
  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-3">
      <div className="flex items-center gap-2">
        <Switch
          id="stage-review-enabled"
          data-testid="stage-review-enabled"
          checked={draft.reviewEnabled}
          onCheckedChange={(checked) => setDraft({ ...draft, reviewEnabled: checked })}
        />
        <Label htmlFor="stage-review-enabled">Review loop</Label>
      </div>
      {draft.reviewEnabled && (
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="stage-review-skill">Review skill</Label>
            <Input
              id="stage-review-skill"
              data-testid="stage-review-skill"
              value={draft.review_skill_name}
              onChange={(e) => setDraft({ ...draft, review_skill_name: e.target.value })}
              placeholder="e.g. code-review"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="stage-review-iterations">Max iterations</Label>
            <Input
              id="stage-review-iterations"
              data-testid="stage-review-iterations"
              type="number"
              min={1}
              max={3}
              value={draft.review_max_iterations}
              onChange={(e) =>
                setDraft({ ...draft, review_max_iterations: e.target.valueAsNumber || 1 })
              }
            />
          </div>
        </div>
      )}
    </div>
  );
}

function ContextStagesFields<T extends StageDraft & { kind: "skill" | "review" }>({
  draft,
  setDraft,
  upstreamNames,
}: {
  draft: T;
  setDraft: (d: T) => void;
  upstreamNames: string[];
}) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Checkbox
          id="stage-context-all"
          data-testid="stage-context-all-upstream"
          checked={draft.contextAllUpstream}
          onCheckedChange={(checked) =>
            setDraft({ ...draft, contextAllUpstream: checked === true })
          }
        />
        <Label htmlFor="stage-context-all">Use all upstream stages as context (default)</Label>
      </div>
      {!draft.contextAllUpstream && (
        <div className="flex flex-col gap-1.5 pl-6">
          {upstreamNames.length === 0 && (
            <p className="text-xs text-muted-foreground">No earlier stages yet.</p>
          )}
          {upstreamNames.map((name) => (
            <div key={name} className="flex items-center gap-2">
              <Checkbox
                id={`stage-context-${name}`}
                data-testid={`stage-context-${name}`}
                checked={draft.context_stages.includes(name)}
                onCheckedChange={(checked) =>
                  setDraft({
                    ...draft,
                    context_stages:
                      checked === true
                        ? [...draft.context_stages, name]
                        : draft.context_stages.filter((n) => n !== name),
                  })
                }
              />
              <Label htmlFor={`stage-context-${name}`}>{name}</Label>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function BoundaryFields<T extends StageDraft & { kind: "skill" | "review" }>({
  draft,
  setDraft,
}: {
  draft: T;
  setDraft: (d: T) => void;
}) {
  const boundary = draft.boundary;
  return (
    <div className="flex flex-col gap-3 rounded-md border border-border p-3">
      <Label>Boundary — what happens after this stage settles</Label>
      <RadioGroup
        data-testid="stage-boundary-mode"
        value={boundary.mode}
        onValueChange={(v) =>
          setDraft({
            ...draft,
            boundary: {
              ...boundary,
              mode: v as "always_hitl" | "always_proceed" | "conditional",
            },
          })
        }
      >
        <div className="flex items-center gap-2">
          <RadioGroupItem value="always_hitl" id="boundary-always-hitl" />
          <Label htmlFor="boundary-always-hitl">Always ask a human</Label>
        </div>
        <div className="flex items-center gap-2">
          <RadioGroupItem value="always_proceed" id="boundary-always-proceed" />
          <Label htmlFor="boundary-always-proceed">Always proceed automatically</Label>
        </div>
        <div className="flex items-center gap-2">
          <RadioGroupItem value="conditional" id="boundary-conditional" />
          <Label htmlFor="boundary-conditional">Conditional</Label>
        </div>
      </RadioGroup>
      {boundary.mode === "conditional" && (
        <div className="flex flex-col gap-2 pl-6">
          <div className="flex items-center gap-2">
            <Checkbox
              id="boundary-blocker"
              data-testid="stage-boundary-on-blocker"
              checked={boundary.on_blocker_residuals}
              onCheckedChange={(checked) =>
                setDraft({
                  ...draft,
                  boundary: { ...boundary, on_blocker_residuals: checked === true },
                })
              }
            />
            <Label htmlFor="boundary-blocker">Pause on open blocker findings</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="boundary-should-fix"
              data-testid="stage-boundary-on-should-fix"
              checked={boundary.on_should_fix_residuals}
              onCheckedChange={(checked) =>
                setDraft({
                  ...draft,
                  boundary: { ...boundary, on_should_fix_residuals: checked === true },
                })
              }
            />
            <Label htmlFor="boundary-should-fix">Pause on open should-fix findings</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="boundary-protected"
              data-testid="stage-boundary-on-protected"
              checked={boundary.on_protected_code}
              onCheckedChange={(checked) =>
                setDraft({
                  ...draft,
                  boundary: { ...boundary, on_protected_code: checked === true },
                })
              }
            />
            <Label htmlFor="boundary-protected">Pause when protected code is touched</Label>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="boundary-confidence">Pause when confidence is below</Label>
            <Select
              value={boundary.on_confidence_below ?? "off"}
              onValueChange={(v) =>
                setDraft({
                  ...draft,
                  boundary: {
                    ...boundary,
                    on_confidence_below: v === "off" ? null : (v as "medium" | "high"),
                  },
                })
              }
            >
              <SelectTrigger id="boundary-confidence" data-testid="stage-boundary-confidence">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="off">Off</SelectItem>
                <SelectItem value="medium">Medium</SelectItem>
                <SelectItem value="high">High</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
      )}
    </div>
  );
}

function ActionFields({
  draft,
  setDraft,
  actions,
}: {
  draft: Extract<StageDraft, { kind: "action" }>;
  setDraft: (d: Extract<StageDraft, { kind: "action" }>) => void;
  actions: ActionInfoView[];
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor="stage-action">Action</Label>
      <Select value={draft.action_id} onValueChange={(v) => setDraft({ ...draft, action_id: v })}>
        <SelectTrigger id="stage-action" data-testid="stage-action">
          <SelectValue placeholder="Choose an action…" />
        </SelectTrigger>
        <SelectContent>
          {actions.map((a) => (
            <SelectItem key={a.action_id} value={a.action_id}>
              {a.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function CallFields({
  draft,
  setDraft,
  pipelineOptions,
}: {
  draft: Extract<StageDraft, { kind: "call" }>;
  setDraft: (d: Extract<StageDraft, { kind: "call" }>) => void;
  pipelineOptions: { id: string; name: string }[];
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor="stage-call-pipeline">Pipeline to call</Label>
      <Select
        value={draft.pipeline_id}
        onValueChange={(v) => setDraft({ ...draft, pipeline_id: v })}
      >
        <SelectTrigger id="stage-call-pipeline" data-testid="stage-call-pipeline">
          <SelectValue placeholder="Choose a pipeline…" />
        </SelectTrigger>
        <SelectContent>
          {pipelineOptions.map((p) => (
            <SelectItem key={p.id} value={p.id}>
              {p.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
