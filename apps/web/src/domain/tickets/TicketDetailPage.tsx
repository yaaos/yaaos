/**
 * Ticket detail — anchor page (E2a.4).
 *
 * Composes the standalone composites:
 *   - StageIndicator
 *   - FindingRow (with inline ack / push-back wired to mutations)
 *   - ActivityEventRow
 *   - HitlPanel
 *
 * Sections:
 *   1. Header band — title + repo + status pill + Cancel/Re-run buttons.
 *   2. Stage indicator.
 *   3. Tab strip — Findings / Activity / HITL.
 *   4. Tab body — depends on active tab.
 *
 * Live updates flow via `useTicket` / `useFindingsForTicket` /
 * `useReviewJobsForTicket` / `useHitlHistory` polling.
 */

import {
  type Ticket,
  useAckFinding,
  useCancelReviewerJobs,
  useFindingsForTicket,
  useHitlHistory,
  useHitlRespond,
  usePushBackFinding,
  useRereviewMutation,
  useReviewJobsForTicket,
  useTicket,
} from "@core/api";
import { useWorkflowActivityStream } from "@core/sse";
import { ConfirmModal, EmptyState, ErrorBanner } from "@shared/components/layout";
import { Button } from "@shared/components/ui/button";
import { Skeleton } from "@shared/components/ui/skeleton";
import { ago } from "@shared/utils/ago";
import { cn } from "@shared/utils/cn";
import { useParams } from "@tanstack/react-router";
import {
  AlertCircle,
  Bell,
  CheckCircle2,
  CircleDashed,
  ListChecks,
  Loader2,
  RotateCcw,
  X,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ActivityEventRow } from "./ActivityEventRow";
import { FindingRow } from "./FindingRow";
import { HitlPanel } from "./HitlPanel";
import { StageIndicator } from "./StageIndicator";

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
  const { ticketId } = useParams({ from: "/orgs/$slug/tickets/$ticketId" });
  const { data: ticket, isLoading, isError, error, refetch } = useTicket(ticketId);
  const [tab, setTab] = useState<Tab>("findings");
  const [showCancel, setShowCancel] = useState(false);
  const [showRerun, setShowRerun] = useState(false);
  const cancel = useCancelReviewerJobs();
  const rereview = useRereviewMutation();

  if (isLoading || !ticket) {
    return (
      <div className="mx-auto max-w-[1100px] px-6 py-6" data-testid="ticket-detail-loading">
        <Skeleton className="h-16 mb-4" />
        <Skeleton className="h-8 mb-4 w-72" />
        <Skeleton className="h-48" />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="mx-auto max-w-[1100px] px-6 py-6">
        <ErrorBanner
          message={(error as Error)?.message || "Couldn't load this ticket."}
          onRetry={() => refetch()}
        />
      </div>
    );
  }

  const status = ticket.status;
  const meta = STATUS_META[status] ?? DEFAULT_STATUS_META;
  const Icon = meta.icon;
  const isTerminal = status === "done" || status === "failed" || status === "cancelled";

  return (
    <div className="mx-auto max-w-[1100px] px-6 py-6" data-testid="ticket-detail">
      <Header
        ticket={ticket}
        status={status}
        meta={meta}
        Icon={Icon}
        onCancel={() => setShowCancel(true)}
        onRerun={() => setShowRerun(true)}
        isTerminal={isTerminal}
        pendingCancel={cancel.isPending}
        pendingRerun={rereview.isPending}
      />

      <StageIndicator stages={ticket.stages} />

      <Tabs tab={tab} onChange={setTab} />

      <div className="mt-4">
        {tab === "findings" && <FindingsTab ticketId={ticketId} />}
        {tab === "activity" && <ActivityTab ticketId={ticketId} />}
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
      <ConfirmModal
        open={showRerun}
        onOpenChange={setShowRerun}
        title="Re-run review?"
        body={
          <>
            <p>Running again spends LLM tokens against the org's BYOK key.</p>
            <p className="mt-2">
              Estimate: ~<span className="font-mono">{estimateTokens(ticket.findings_count)}</span>{" "}
              tokens ( ≈$<span className="font-mono">{estimateUsd(ticket.findings_count)}</span> at
              default Sonnet rates). Existing findings persist; the agents look at the latest
              commit.
            </p>
          </>
        }
        confirmLabel="Re-run"
        pending={rereview.isPending}
        onConfirm={() => {
          rereview.mutate(ticketId, { onSettled: () => setShowRerun(false) });
        }}
      />
    </div>
  );
}

/**
 * Heuristic token-spend estimate for the re-run modal. POC stand-in for
 * a real per-org / per-model integration with the BYOK provider — keeps
 * the spec promise ("cost-protective modal") while avoiding fake
 * precision. Scales with findings count since more findings → bigger
 * conversation context.
 */
function estimateTokens(findingsCount: number): string {
  const baseline = 15_000;
  const perFinding = 4_000;
  const est = baseline + Math.max(0, findingsCount) * perFinding;
  return est.toLocaleString();
}

function estimateUsd(findingsCount: number): string {
  // Claude Sonnet input ≈ $3 / 1M tokens, output ≈ $15 / 1M. Blended
  // POC midpoint: $5 / 1M.
  const baseline = 15_000;
  const perFinding = 4_000;
  const tokens = baseline + Math.max(0, findingsCount) * perFinding;
  const usd = (tokens / 1_000_000) * 5;
  return usd < 0.1 ? usd.toFixed(2) : usd.toFixed(2);
}

function Header({
  ticket,
  status,
  meta,
  Icon,
  onCancel,
  onRerun,
  isTerminal,
  pendingCancel,
  pendingRerun,
}: {
  ticket: Ticket;
  status: string;
  meta: { label: string; chip: string };
  Icon: typeof Loader2;
  onCancel: () => void;
  onRerun: () => void;
  isTerminal: boolean;
  pendingCancel: boolean;
  pendingRerun: boolean;
}) {
  const builder = ticket.builder ?? {
    kind: ticket.builder_kind,
    display_name: ticket.builder_display_name ?? ticket.author_login ?? null,
  };
  return (
    <header className="flex items-start justify-between gap-4 mb-4">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 text-xs text-muted-foreground mono mb-1">
          <span>{ticket.repo_external_id}</span>
          {ticket.pr_number != null && (
            <>
              <span>·</span>
              {ticket.pr_html_url ? (
                <a
                  href={ticket.pr_html_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:text-foreground"
                >
                  PR #{ticket.pr_number}
                </a>
              ) : (
                <span>PR #{ticket.pr_number}</span>
              )}
            </>
          )}
          <span>·</span>
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
            <Icon className={cn("w-3 h-3", status === "running" && "animate-spin")} />
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
        <Button
          variant="default"
          onClick={onRerun}
          disabled={pendingRerun}
          data-testid="ticket-rerun-button"
        >
          <RotateCcw className="w-3.5 h-3.5" />
          {isTerminal ? "Re-run review" : "Re-run"}
        </Button>
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
  const { data: findings, isLoading } = useFindingsForTicket(ticketId, true);
  const ack = useAckFinding(ticketId);
  const pushBack = usePushBackFinding(ticketId);
  if (isLoading) return <Skeleton className="h-24" />;
  if (!findings || findings.length === 0) {
    return (
      <EmptyState
        icon={ListChecks}
        headline="No findings yet."
        body="When yaaos surfaces something to review, it'll appear here."
      />
    );
  }
  return (
    <div className="flex flex-col gap-2" data-testid="findings-list">
      {findings.map((f) => (
        <FindingRow
          key={f.id}
          finding={f}
          onAck={(id) => ack.mutate(id)}
          onPushBack={(args) => pushBack.mutate(args)}
          pending={ack.isPending || pushBack.isPending}
        />
      ))}
    </div>
  );
}

function ActivityTab({ ticketId }: { ticketId: string }) {
  const { data: jobs, isLoading } = useReviewJobsForTicket(ticketId);
  const { data: ticket } = useTicket(ticketId);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const events = (jobs ?? []).flatMap((j) => j.activity_log ?? []);
  // Latest workflow execution drives the demand-pull live stream — opening
  // this EventSource sends `track()` to SubscriberRegistry which fans
  // `subscribe` over the agent WebSocket, so the WorkspaceAgent only emits
  // activity while this tab is mounted.
  const latestWorkflowId =
    ticket?.stages && ticket.stages.length > 0
      ? ticket.stages[ticket.stages.length - 1]?.workflow_execution_id
      : null;
  const liveEvents = useWorkflowActivityStream(latestWorkflowId);
  // Merge stored + live, dedupe by ts+kind, sort chronologically.
  const seen = new Set<string>();
  const ordered = [...events, ...liveEvents]
    .filter((e) => {
      const k = `${e.ts ?? ""}-${e.kind ?? ""}`;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    })
    .sort((a, b) => (a.ts ?? "").localeCompare(b.ts ?? ""));
  const newestTs = ordered.length > 0 ? ordered[ordered.length - 1]?.ts : null;

  // Auto-scroll to the newest event when it changes — so a long-running
  // review keeps the latest step visible without manual scroll-down.
  useEffect(() => {
    if (newestTs && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [newestTs]);

  if (isLoading) return <Skeleton className="h-24" />;
  if (ordered.length === 0) {
    return (
      <EmptyState
        icon={AlertCircle}
        headline="No activity yet."
        body="As the reviewer runs, its steps stream in here."
      />
    );
  }
  return (
    <div
      className="rounded-md border border-border max-h-[600px] overflow-y-auto"
      data-testid="activity-stream"
    >
      {ordered.map((e) => (
        <ActivityEventRow key={`${e.ts}-${e.kind}`} event={e} />
      ))}
      <div ref={bottomRef} aria-hidden />
    </div>
  );
}

function HitlTab({ ticketId }: { ticketId: string }) {
  const { data: history, isLoading } = useHitlHistory(ticketId);
  const respond = useHitlRespond(ticketId);
  if (isLoading) return <Skeleton className="h-24" />;
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
