/**
 * Ticket detail — shows the workflow run step tree (live for the running
 * `CodeReview` step, persisted accordion when done), non-interactive
 * findings, and HITL history.
 *
 * Sections:
 *   1. Header band — title + status pill + Cancel button.
 *   2. Stage indicator (workflow-run band, sourced from useWorkflowRuns).
 *   3. Tab strip — Findings / Activity / HITL.
 *   4. Tab body.
 *
 * Live updates:
 *   - `workflow_state_changed` SSE → invalidates ["workflow","runs",ticketId]
 *     + ["tickets",ticketId] — step tree and stage band refresh automatically.
 *   - Live `CodeReview` step: `useWorkflowActivityStream(execution_id)`.
 *   - Terminal `CodeReview` step: `useStepActivity` accordion.
 */

import type { Ticket } from "@core/api/public/client";
import {
  type WorkflowRunView,
  useCancelReviewerJobs,
  useFindingsForTicket,
  useHitlHistory,
  useHitlRespond,
  useStepActivity,
  useTicket,
  useWorkflowRuns,
} from "@core/api/public/queries";
import { useWorkflowActivityStream } from "@core/sse/public/workflow_activity";
import { ConfirmModal } from "@shared/components/public/layout/confirm-modal";
import { EmptyState } from "@shared/components/public/layout/empty-state";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { Button } from "@shared/components/ui/button";
import { Skeleton } from "@shared/components/ui/skeleton";
import { ago } from "@shared/utils/public/ago";
import { cn } from "@shared/utils/public/cn";
import { useParams } from "@tanstack/react-router";
import { AlertCircle, Bell, CheckCircle2, CircleDashed, Loader2, X, XCircle } from "lucide-react";
import { Suspense, useEffect, useRef, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { ActivityEventRow } from "../ActivityEventRow";
import { FindingRow } from "../FindingRow";
import { HitlPanel } from "../HitlPanel";
import { StageIndicator } from "../StageIndicator";

type Tab = "findings" | "activity" | "hitl";

interface StatusMeta {
  label: string;
  icon: typeof Loader2;
  chip: string;
}

// Solid semantic-color chips pass WCAG AA contrast against the matching
// `*-foreground` pair (see TicketsListPage for the same rationale).
const DEFAULT_STATUS_META: StatusMeta = {
  label: "Running",
  icon: Loader2,
  chip: "bg-info text-info-foreground border-info",
};

const STATUS_META: Record<string, StatusMeta> = {
  pending: { label: "Queued", icon: Loader2, chip: "bg-muted text-muted-foreground border-border" },
  running: DEFAULT_STATUS_META,
  hitl: { label: "HITL", icon: Bell, chip: "bg-warning text-warning-foreground border-warning" },
  done: {
    label: "Done",
    icon: CheckCircle2,
    chip: "bg-success text-success-foreground border-success",
  },
  failed: {
    label: "Failed",
    icon: XCircle,
    chip: "bg-destructive text-destructive-foreground border-destructive",
  },
  cancelled: {
    label: "Cancelled",
    icon: CircleDashed,
    chip: "bg-muted text-muted-foreground border-border",
  },
};

export function TicketDetailPage() {
  return (
    <div className="mx-auto max-w-[1100px] px-6 py-6">
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load this ticket." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense
          fallback={
            <div data-testid="ticket-detail-loading">
              <Skeleton className="h-16 mb-4" />
              <Skeleton className="h-8 mb-4 w-72" />
              <Skeleton className="h-48" />
            </div>
          }
        >
          <TicketDetailContent />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}

function TicketDetailContent() {
  const { ticketId } = useParams({ from: "/org/$slug/tickets/$ticketId" });
  const { data: ticket } = useTicket(ticketId);
  const { data: runs } = useWorkflowRuns(ticketId);
  const [tab, setTab] = useState<Tab>("findings");
  const [showCancel, setShowCancel] = useState(false);
  const cancel = useCancelReviewerJobs();

  const status = ticket.status;
  const meta = STATUS_META[status] ?? DEFAULT_STATUS_META;
  const Icon = meta.icon;
  const isTerminal = status === "done" || status === "failed" || status === "cancelled";

  return (
    <div data-testid="ticket-detail">
      <Header
        ticket={ticket}
        status={status}
        meta={meta}
        Icon={Icon}
        onCancel={() => setShowCancel(true)}
        isTerminal={isTerminal}
        pendingCancel={cancel.isPending}
      />

      <StageIndicator runs={runs} />

      <Tabs tab={tab} onChange={setTab} />

      <div className="mt-4">
        {tab === "findings" && <FindingsTab ticketId={ticketId} />}
        {tab === "activity" && <ActivityTab ticketId={ticketId} runs={runs ?? []} />}
        {tab === "hitl" && <HitlTab ticketId={ticketId} />}
      </div>

      <ConfirmModal
        open={showCancel}
        onOpenChange={setShowCancel}
        title="Cancel review?"
        body="The current review stops at its next safe checkpoint. Findings already posted stay."
        confirmLabel="Cancel review"
        tone="destructive"
        pending={cancel.isPending}
        onConfirm={() => {
          cancel.mutate(ticketId, { onSettled: () => setShowCancel(false) });
        }}
      />
    </div>
  );
}

function Header({
  ticket,
  status,
  meta,
  Icon,
  onCancel,
  isTerminal,
  pendingCancel,
}: {
  ticket: Ticket;
  status: string;
  meta: { label: string; chip: string };
  Icon: typeof Loader2;
  onCancel: () => void;
  isTerminal: boolean;
  pendingCancel: boolean;
}) {
  const builder = ticket.builder ?? {
    kind: ticket.builder_kind,
    display_name: ticket.builder_display_name ?? ticket.author_login ?? null,
  };
  return (
    <header className="flex items-start justify-between gap-4 mb-4">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 text-xs text-muted-foreground mono mb-1">
          <span>updated {ago(ticket.updated_at)}</span>
        </div>
        <h1 className="text-2xl font-semibold tracking-tight">{ticket.title}</h1>
        <div className="flex items-center gap-2 mt-2 text-sm">
          <span
            className={cn(
              "inline-flex items-center gap-1.5 h-5 px-2 rounded text-[10.5px] font-medium border",
              meta.chip,
            )}
            data-testid={`ticket-status-${status}`}
          >
            <Icon
              className={cn(
                "w-3 h-3",
                (status === "running" || status === "pending") && "animate-spin",
              )}
            />
            {meta.label}
          </span>
          <span className="text-muted-foreground">
            by {builder.kind === "system" ? "yaaos" : (builder.display_name ?? "—")}
          </span>
        </div>
      </div>
      <div className="flex gap-2 pt-1 shrink-0">
        {!isTerminal && (
          <Button
            variant="outline"
            onClick={onCancel}
            disabled={pendingCancel}
            data-testid="ticket-cancel-button"
          >
            <X className="w-3.5 h-3.5" />
            Cancel
          </Button>
        )}
      </div>
    </header>
  );
}

function Tabs({ tab, onChange }: { tab: Tab; onChange: (t: Tab) => void }) {
  const items: Array<{ id: Tab; label: string }> = [
    { id: "findings", label: "Findings" },
    { id: "activity", label: "Activity" },
    { id: "hitl", label: "HITL" },
  ];
  return (
    <nav className="flex items-center gap-1 border-b border-border" role="tablist">
      {items.map((it) => {
        const active = tab === it.id;
        return (
          <button
            key={it.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(it.id)}
            data-testid={`ticket-tab-${it.id}`}
            className={cn(
              "px-3 h-9 text-sm border-b-2 -mb-px transition-colors",
              active
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {it.label}
          </button>
        );
      })}
    </nav>
  );
}

function FindingsTab({ ticketId }: { ticketId: string }) {
  return (
    <ErrorBoundary
      fallbackRender={({ resetErrorBoundary }) => (
        <ErrorBanner message="Couldn't load findings." onRetry={resetErrorBoundary} />
      )}
    >
      <Suspense fallback={<Skeleton className="h-24" />}>
        <FindingsTabContent ticketId={ticketId} />
      </Suspense>
    </ErrorBoundary>
  );
}

function FindingsTabContent({ ticketId }: { ticketId: string }) {
  const { data: findings } = useFindingsForTicket(ticketId, true);
  if (!findings || findings.length === 0) {
    return (
      <EmptyState
        icon={AlertCircle}
        headline="No findings yet."
        body="When yaaos surfaces something to review, it'll appear here."
      />
    );
  }
  return (
    <div className="flex flex-col gap-2" data-testid="findings-list">
      {findings.map((f) => (
        <FindingRow key={f.id} finding={f} />
      ))}
    </div>
  );
}

// ── Activity tab: step tree ────────────────────────────────────────────────

/**
 * Canonical step label for a non-CodeReview step: name · state · timings.
 * No expandable content — these are control-plane coordination steps.
 */
function StepLabel({
  step,
  ticketId,
  executionId,
}: {
  step: WorkflowRunView["steps"][number];
  ticketId: string;
  executionId: string;
}) {
  const isInvokeStep = step.command_kind === "InvokeClaudeCode";
  const isRunning = step.state === "running";
  const isDone = step.state === "done" || step.state === "failed";

  if (isInvokeStep) {
    if (isRunning) {
      return <LiveCodeReviewStep executionId={executionId} />;
    }
    if (isDone) {
      return (
        <TerminalCodeReviewStep
          ticketId={ticketId}
          executionId={executionId}
          stepId={step.step_id}
          state={step.state}
        />
      );
    }
  }

  // Non-CodeReview steps: compact label row.
  type StepIconMeta = { icon: typeof Loader2; tone: string; spin: boolean };
  const STEP_PENDING: StepIconMeta = {
    icon: CircleDashed,
    tone: "text-muted-foreground",
    spin: false,
  };
  const stateIcon: Record<string, StepIconMeta> = {
    pending: STEP_PENDING,
    running: { icon: Loader2, tone: "text-info", spin: true },
    done: { icon: CheckCircle2, tone: "text-success", spin: false },
    failed: { icon: XCircle, tone: "text-destructive", spin: false },
    skipped: STEP_PENDING,
  };
  const s: StepIconMeta = stateIcon[step.state] ?? STEP_PENDING;
  const Icon = s.icon;

  return (
    <div
      className="flex items-center gap-2 px-3 py-2 border-b border-border last:border-0 text-sm"
      data-testid={`step-row-${step.step_id}`}
    >
      <Icon className={cn("w-3.5 h-3.5 shrink-0", s.tone, s.spin && "animate-spin")} aria-hidden />
      <span className="font-medium text-foreground">{step.command_kind}</span>
      <span className="text-muted-foreground">·</span>
      <span className="text-muted-foreground capitalize">{step.state}</span>
      {step.started_at && (
        <>
          <span className="text-muted-foreground">·</span>
          <span className="text-xs text-muted-foreground mono">{ago(step.started_at)}</span>
        </>
      )}
    </div>
  );
}

/**
 * Running `InvokeClaudeCode` step: pinned ScrollArea with live activity stream.
 * Opening this EventSource tells the backend's SubscriberRegistry to start
 * forwarding WorkspaceAgent activity batches for this execution.
 */
function LiveCodeReviewStep({ executionId }: { executionId: string }) {
  const liveEvents = useWorkflowActivityStream(executionId);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to newest event. `liveEvents.length` is the logical trigger but
  // Biome's exhaustive-deps sees the array reference as unneeded. We read the
  // length inside the effect so the dep is genuinely observed.
  useEffect(() => {
    const _len = liveEvents.length; // consumed so biome sees the dep
    if (_len > 0 && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [liveEvents]);

  return (
    <div
      className="flex flex-col px-3 py-2 border-b border-border last:border-0"
      data-testid="step-code-review-live"
    >
      <div className="flex items-center gap-2 text-sm mb-2">
        <Loader2 className="w-3.5 h-3.5 shrink-0 text-info animate-spin" aria-hidden />
        <span className="font-medium">CodeReview</span>
        <span className="text-muted-foreground">·</span>
        <span className="text-muted-foreground">Running</span>
      </div>
      {liveEvents.length > 0 ? (
        <div
          className="max-h-[400px] overflow-y-auto rounded border border-border"
          data-testid="activity-stream"
        >
          {liveEvents.map((e) => (
            <ActivityEventRow key={`${e.ts}-${e.kind}`} event={e} />
          ))}
          <div ref={bottomRef} aria-hidden />
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">Waiting for agent activity…</p>
      )}
    </div>
  );
}

/**
 * Terminal `InvokeClaudeCode` step: accordion that lazy-loads the persisted
 * ActivityLog blob via `useStepActivity`. The accordion opens on click;
 * the inner Suspense fetches the blob on first open.
 */
function TerminalCodeReviewStep({
  ticketId,
  executionId,
  stepId,
  state,
}: {
  ticketId: string;
  executionId: string;
  stepId: string;
  state: string;
}) {
  const stateIcon =
    state === "done"
      ? { icon: CheckCircle2, tone: "text-success" }
      : { icon: XCircle, tone: "text-destructive" };
  const Icon = stateIcon.icon;

  return (
    <details className="border-b border-border last:border-0" data-testid="step-code-review-done">
      <summary className="flex items-center gap-2 px-3 py-2 cursor-pointer text-sm list-none select-none hover:bg-accent/40">
        <Icon className={cn("w-3.5 h-3.5 shrink-0", stateIcon.tone)} aria-hidden />
        <span className="font-medium">CodeReview</span>
        <span className="text-muted-foreground">·</span>
        <span className="text-muted-foreground capitalize">{state}</span>
        <span className="ml-auto text-xs text-muted-foreground">Click to expand</span>
      </summary>
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load activity." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense fallback={<Skeleton className="h-16 m-3" />}>
          <StepActivityContent ticketId={ticketId} executionId={executionId} stepId={stepId} />
        </Suspense>
      </ErrorBoundary>
    </details>
  );
}

function StepActivityContent({
  ticketId,
  executionId,
  stepId,
}: {
  ticketId: string;
  executionId: string;
  stepId: string;
}) {
  const { data } = useStepActivity(ticketId, executionId, stepId);
  const activity = data?.activity;

  if (activity === null || activity === undefined) {
    return (
      <p className="px-3 py-2 text-xs text-muted-foreground italic">
        Activity log expired or unavailable.
      </p>
    );
  }

  const events = activity.events;

  if (events.length === 0) {
    return <p className="px-3 py-2 text-xs text-muted-foreground italic">No activity recorded.</p>;
  }

  return (
    <div className="max-h-[400px] overflow-y-auto" data-testid="step-activity-blob">
      {events.map((ev, i) => (
        <ActivityEventRow
          key={`${ev.seq}-${i}`}
          event={{
            ts: ev.ts,
            kind: ev.kind,
            // A blank message would render as an empty row; surface the gap.
            message: ev.message || "(no message)",
            detail: ev.detail ?? null,
          }}
        />
      ))}
    </div>
  );
}

function ActivityTab({
  ticketId,
  runs,
}: {
  ticketId: string;
  runs: WorkflowRunView[];
}) {
  return (
    <ErrorBoundary
      fallbackRender={({ resetErrorBoundary }) => (
        <ErrorBanner message="Couldn't load activity." onRetry={resetErrorBoundary} />
      )}
    >
      <ActivityTabContent ticketId={ticketId} runs={runs} />
    </ErrorBoundary>
  );
}

function ActivityTabContent({
  ticketId,
  runs,
}: {
  ticketId: string;
  runs: WorkflowRunView[];
}) {
  if (runs.length === 0) {
    return (
      <EmptyState
        icon={AlertCircle}
        headline="No activity yet."
        body="As the reviewer runs, its steps stream in here."
      />
    );
  }

  // Show the most recent run's step tree. Runs arrive oldest-first so the
  // last entry is the most recent.
  const latestRun = runs[runs.length - 1];

  return (
    <div className="rounded-md border border-border" data-testid="step-tree">
      {latestRun?.steps.map((step) => (
        <StepLabel key={step.step_id} step={step} ticketId={ticketId} executionId={latestRun.id} />
      ))}
    </div>
  );
}

function HitlTab({ ticketId }: { ticketId: string }) {
  return (
    <ErrorBoundary
      fallbackRender={({ resetErrorBoundary }) => (
        <ErrorBanner message="Couldn't load HITL history." onRetry={resetErrorBoundary} />
      )}
    >
      <Suspense fallback={<Skeleton className="h-24" />}>
        <HitlTabContent ticketId={ticketId} />
      </Suspense>
    </ErrorBoundary>
  );
}

function HitlTabContent({ ticketId }: { ticketId: string }) {
  const { data: history } = useHitlHistory(ticketId);
  const respond = useHitlRespond(ticketId);
  const open = (history ?? []).find((h) => !h.resolved_at);
  const past = (history ?? []).filter((h) => h.resolved_at);

  if (!open && past.length === 0) {
    return (
      <EmptyState
        icon={Bell}
        headline="No HITL exchanges yet."
        body="If the workflow needs a decision, the prompt shows up here."
      />
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="hitl-tab">
      {open && (
        <HitlPanel
          payload={open.question_payload}
          onSubmit={(response) => respond.mutate(response)}
          pending={respond.isPending}
        />
      )}
      {past.length > 0 && (
        <section>
          <h3 className="text-sm font-medium mb-2 text-muted-foreground">History</h3>
          <ul className="flex flex-col gap-2" data-testid="hitl-history-list">
            {past.map((h) => (
              <li
                key={h.id}
                className="border border-border rounded-md p-3 text-sm"
                data-testid={`hitl-history-${h.id}`}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="font-medium">
                    {(h.question_payload as { title?: string }).title ?? "HITL prompt"}
                  </span>
                  <span className="text-xs text-muted-foreground mono shrink-0">
                    resolved {h.resolved_at ? ago(h.resolved_at) : "—"}
                  </span>
                </div>
                {h.resolution_payload && (
                  <pre className="mt-2 text-xs whitespace-pre-wrap text-muted-foreground">
                    {JSON.stringify(h.resolution_payload, null, 2)}
                  </pre>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
