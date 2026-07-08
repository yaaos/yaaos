/**
 * Triggers section — a repo's intake→pipeline bindings. Each row is
 * committed independently (`POST`/`DELETE /api/repos/triggers`) — unlike
 * protected-code/auto-approval, bindings are not part of the settings
 * whole-section replace.
 */

import { apiErrorCode } from "@core/api/public/client";
import {
  type IntakePointView,
  type PipelineSummaryView,
  type TriggerBindingView,
  useAddTrigger,
  useRemoveTrigger,
} from "@core/api/public/queries";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@shared/components/ui/select";
import { Textarea } from "@shared/components/ui/textarea";
import { Plus, X } from "lucide-react";
import { useState } from "react";
import { UserMultiSelect } from "./UserMultiSelect";
import type { OrgMemberSummary } from "./queries";

interface TriggerDraft {
  intake_point_id: string;
  pipeline_id: string;
  schedule_name: string;
  schedule_cron: string;
  schedule_notify_user_ids: string[];
  schedule_kickoff_input: string;
}

function emptyTriggerDraft(): TriggerDraft {
  return {
    intake_point_id: "",
    pipeline_id: "",
    schedule_name: "",
    schedule_cron: "",
    schedule_notify_user_ids: [],
    schedule_kickoff_input: "",
  };
}

function triggerDraftIsValid(draft: TriggerDraft, point: IntakePointView | undefined): boolean {
  if (!draft.intake_point_id || !draft.pipeline_id || !point) return false;
  if (point.kind === "schedule") {
    return (
      draft.schedule_name.trim().length > 0 &&
      draft.schedule_cron.trim().length > 0 &&
      draft.schedule_notify_user_ids.length > 0
    );
  }
  return true;
}

function addTriggerErrorMessage(err: unknown): string {
  const code = apiErrorCode(err);
  if (code === "unknown_point") return "Unknown intake point.";
  if (code === "invalid_cron") return "That cron expression doesn't parse.";
  if (code === "invalid_schedule") return "Schedule is missing required fields.";
  if (code === "pipeline_not_found") return "That pipeline doesn't belong to this org.";
  if (code === "duplicate_binding") return "This repo already has a trigger for that intake point.";
  return "Couldn't add this trigger.";
}

export function TriggersSection({
  repoExternalId,
  bindings,
  intakePoints,
  pipelines,
  members,
}: {
  repoExternalId: string;
  bindings: TriggerBindingView[];
  intakePoints: IntakePointView[];
  pipelines: PipelineSummaryView[];
  members: OrgMemberSummary[];
}) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState<TriggerDraft>(emptyTriggerDraft());
  const add = useAddTrigger(repoExternalId);
  const remove = useRemoveTrigger(repoExternalId);

  const selectedPoint = intakePoints.find((p) => p.id === draft.intake_point_id);

  function submit() {
    if (!selectedPoint) return;
    add.mutate(
      {
        intake_point_id: draft.intake_point_id,
        pipeline_id: draft.pipeline_id,
        schedule:
          selectedPoint.kind === "schedule"
            ? {
                name: draft.schedule_name,
                cron: draft.schedule_cron,
                notify_user_ids: draft.schedule_notify_user_ids,
                kickoff_input: draft.schedule_kickoff_input || null,
              }
            : null,
      },
      {
        onSuccess: () => {
          setAdding(false);
          setDraft(emptyTriggerDraft());
        },
      },
    );
  }

  return (
    <div className="flex flex-col gap-3" data-testid={`repo-triggers-${repoExternalId}`}>
      <h3 className="text-sm font-semibold">Triggers</h3>

      {bindings.length === 0 && !adding && (
        <p className="text-sm text-muted-foreground" data-testid="repo-triggers-empty">
          No triggers. Nothing runs for this repo.
        </p>
      )}

      <div className="flex flex-col gap-2">
        {bindings.map((binding) => {
          const point = intakePoints.find((p) => p.id === binding.intake_point_id);
          return (
            <div
              key={binding.id}
              data-testid={`repo-trigger-row-${binding.id}`}
              className="flex flex-col gap-1 rounded-md border border-border px-3 py-2 text-sm"
            >
              <div className="flex items-center gap-2">
                <Badge variant="outline">{point?.label ?? binding.intake_point_id}</Badge>
                <span className="flex-1 truncate">{binding.pipeline_name}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label="Remove trigger"
                  data-testid={`repo-trigger-remove-${binding.id}`}
                  onClick={() => remove.mutate(binding.id)}
                >
                  <X className="w-4 h-4" aria-hidden />
                </Button>
              </div>
              {binding.schedule && (
                <p className="text-xs text-muted-foreground">
                  {binding.schedule.name} · {binding.schedule.cron} ·{" "}
                  {binding.schedule.notify_user_ids.length} notified
                </p>
              )}
            </div>
          );
        })}
      </div>

      {adding ? (
        <div
          className="flex flex-col gap-3 rounded-md border border-border p-3"
          data-testid="repo-trigger-form"
        >
          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`trigger-point-${repoExternalId}`}>Intake point</Label>
              <Select
                value={draft.intake_point_id}
                onValueChange={(v) => setDraft({ ...draft, intake_point_id: v })}
              >
                <SelectTrigger
                  id={`trigger-point-${repoExternalId}`}
                  data-testid="repo-trigger-intake-point"
                >
                  <SelectValue placeholder="Choose an intake point…" />
                </SelectTrigger>
                <SelectContent>
                  {intakePoints.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`trigger-pipeline-${repoExternalId}`}>Pipeline</Label>
              <Select
                value={draft.pipeline_id}
                onValueChange={(v) => setDraft({ ...draft, pipeline_id: v })}
              >
                <SelectTrigger
                  id={`trigger-pipeline-${repoExternalId}`}
                  data-testid="repo-trigger-pipeline"
                >
                  <SelectValue placeholder="Choose a pipeline…" />
                </SelectTrigger>
                <SelectContent>
                  {pipelines.map((p) => (
                    <SelectItem key={p.id} value={p.id}>
                      {p.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {selectedPoint?.kind === "schedule" && (
            <div className="flex flex-col gap-3 rounded-md border border-border p-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="trigger-schedule-name">Name</Label>
                <Input
                  id="trigger-schedule-name"
                  data-testid="repo-trigger-schedule-name"
                  value={draft.schedule_name}
                  onChange={(e) => setDraft({ ...draft, schedule_name: e.target.value })}
                  placeholder="e.g. nightly sweep"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="trigger-schedule-cron">Cron (UTC)</Label>
                <Input
                  id="trigger-schedule-cron"
                  data-testid="repo-trigger-schedule-cron"
                  value={draft.schedule_cron}
                  onChange={(e) => setDraft({ ...draft, schedule_cron: e.target.value })}
                  placeholder="0 3 * * *"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="trigger-schedule-notify">Notify</Label>
                <UserMultiSelect
                  members={members}
                  selected={draft.schedule_notify_user_ids}
                  onChange={(ids) => setDraft({ ...draft, schedule_notify_user_ids: ids })}
                  placeholder="Choose who to notify…"
                  data-testid="repo-trigger-schedule-notify"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="trigger-schedule-kickoff">Kickoff input</Label>
                <Textarea
                  id="trigger-schedule-kickoff"
                  data-testid="repo-trigger-schedule-kickoff"
                  value={draft.schedule_kickoff_input}
                  onChange={(e) => setDraft({ ...draft, schedule_kickoff_input: e.target.value })}
                  placeholder="Optional text handed to the first stage"
                />
              </div>
            </div>
          )}

          <div className="flex items-center gap-2">
            <Button
              data-testid="repo-trigger-save"
              disabled={!triggerDraftIsValid(draft, selectedPoint) || add.isPending}
              onClick={submit}
            >
              {add.isPending ? "Saving…" : "Add trigger"}
            </Button>
            <Button
              variant="outline"
              onClick={() => {
                setAdding(false);
                setDraft(emptyTriggerDraft());
              }}
            >
              Cancel
            </Button>
          </div>
          {add.isError && <ErrorBanner message={addTriggerErrorMessage(add.error)} />}
        </div>
      ) : (
        <Button
          variant="outline"
          size="sm"
          className="self-start"
          data-testid="repo-add-trigger"
          onClick={() => setAdding(true)}
        >
          <Plus className="w-4 h-4" aria-hidden />
          Add trigger
        </Button>
      )}
    </div>
  );
}
