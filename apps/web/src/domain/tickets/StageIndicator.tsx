/**
 * Stage indicator — visualizes the ticket's current and historical workflow
 * runs as a stage band.
 *
 * Ticket detail header band. Single-run tickets render as a compact
 * "Stage: <name> · <state>" line; multi-run tickets show each run in order
 * with the active one highlighted.
 *
 * Source of truth: `WorkflowRunView[]` from `GET /api/tickets/{id}/workflow-runs`,
 * fetched via `useWorkflowRuns`. When the array is empty the component renders
 * nothing so callers can drop it in safely.
 */

import type { WorkflowRunView } from "@core/api/public/queries";
import { cn } from "@shared/utils/public/cn";
import { CheckCircle2, CircleDashed, Loader2, XCircle } from "lucide-react";

interface StateMeta {
  label: string;
  icon: typeof Loader2;
  tone: string;
}

const DEFAULT_META: StateMeta = { label: "Running", icon: Loader2, tone: "text-info" };

const STATE_META: Record<string, StateMeta> = {
  running: DEFAULT_META,
  awaiting_agent: DEFAULT_META,
  awaiting_human: { label: "Awaiting human", icon: Loader2, tone: "text-warning" },
  done: { label: "Done", icon: CheckCircle2, tone: "text-success" },
  failed: { label: "Failed", icon: XCircle, tone: "text-destructive" },
  cancelled: { label: "Cancelled", icon: CircleDashed, tone: "text-muted-foreground" },
};

function metaFor(state: string): StateMeta {
  return STATE_META[state] ?? DEFAULT_META;
}

export function StageIndicator({ runs }: { runs: WorkflowRunView[] | undefined }) {
  if (!runs || runs.length === 0) return null;

  // Runs arrive oldest-first from the API — display chronologically left to right.
  return (
    <div
      className="flex items-center gap-2 text-sm"
      data-testid="stage-indicator"
      aria-label="Workflow stages"
    >
      {runs.map((run, i) => {
        const meta = metaFor(run.state);
        const Icon = meta.icon;
        const spin =
          run.state === "running" ||
          run.state === "awaiting_agent" ||
          run.state === "awaiting_human";
        return (
          <div key={run.id} className="flex items-center gap-2">
            {i > 0 && (
              <span className="text-muted-foreground" aria-hidden="true">
                →
              </span>
            )}
            <span
              className={cn(
                "inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs",
                "border border-border bg-secondary",
              )}
              data-testid={`stage-${run.workflow_name}`}
            >
              {/* Icon carries the semantic-color hint; the label text uses
                  the default foreground so contrast stays >=4.5:1 against
                  bg-secondary even at the chip's small text size. */}
              <Icon
                className={cn("w-3 h-3", meta.tone, spin && "animate-spin")}
                aria-hidden="true"
              />
              <span className="font-medium">{run.workflow_name}</span>
              <span className="text-muted-foreground">·</span>
              <span>{meta.label}</span>
            </span>
          </div>
        );
      })}
    </div>
  );
}
