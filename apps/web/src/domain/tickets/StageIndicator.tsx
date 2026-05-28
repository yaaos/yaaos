/**
 * Stage indicator — visualizes the ticket's current and historical stages.
 *
 * Ticket detail header band. Single-stage tickets render as a
 * compact "Stage: <name> · <state>" line; multi-stage tickets show each
 * stage in order with the active one highlighted.
 *
 * Source of truth: the `stages` array on the extended `GET /api/tickets/{id}`
 * response. When the field is absent
 * (older ticket payloads or partially populated cache entries) the component
 * renders nothing so callers can drop it in safely.
 */

import { cn } from "@shared/utils/cn";
import { CheckCircle2, CircleDashed, Loader2, XCircle } from "lucide-react";

export interface TicketStage {
  name: string;
  state: string;
  attempt_count: number;
  current_attempt: number;
  started_at: string | null;
  completed_at: string | null;
  workflow_execution_id: string;
}

interface StateMeta {
  label: string;
  icon: typeof Loader2;
  tone: string;
}

const DEFAULT_META: StateMeta = { label: "Running", icon: Loader2, tone: "text-info" };

const STATE_META: Record<string, StateMeta> = {
  running: DEFAULT_META,
  awaiting_human: { label: "Awaiting human", icon: Loader2, tone: "text-warning" },
  done: { label: "Done", icon: CheckCircle2, tone: "text-success" },
  failed: { label: "Failed", icon: XCircle, tone: "text-destructive" },
  cancelled: { label: "Cancelled", icon: CircleDashed, tone: "text-muted-foreground" },
};

function metaFor(state: string): StateMeta {
  return STATE_META[state] ?? DEFAULT_META;
}

export function StageIndicator({ stages }: { stages: TicketStage[] | undefined }) {
  if (!stages || stages.length === 0) return null;

  // Backend returns newest-first; reverse for chronological left-to-right.
  const ordered = [...stages].reverse();

  return (
    <div
      className="flex items-center gap-2 text-sm"
      data-testid="stage-indicator"
      aria-label="Workflow stages"
    >
      {ordered.map((stage, i) => {
        const meta = metaFor(stage.state);
        const Icon = meta.icon;
        const spin = stage.state === "running" || stage.state === "awaiting_human";
        return (
          <div key={`${stage.workflow_execution_id}-${i}`} className="flex items-center gap-2">
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
              data-testid={`stage-${stage.name}`}
            >
              {/* Icon carries the semantic-color hint; the label text uses
                  the default foreground so contrast stays >=4.5:1 against
                  bg-secondary even at the chip's small text size. */}
              <Icon
                className={cn("w-3 h-3", meta.tone, spin && "animate-spin")}
                aria-hidden="true"
              />
              <span className="font-medium">{stage.name}</span>
              <span className="text-muted-foreground">·</span>
              <span>{meta.label}</span>
              {stage.attempt_count > 1 && (
                <span className="text-muted-foreground">
                  · Attempt {stage.current_attempt}/{stage.attempt_count}
                </span>
              )}
            </span>
          </div>
        );
      })}
    </div>
  );
}
