/**
 * Ticket detail — M06 anchor page (E2a.4).
 *
 * Composes the standalone composites built earlier in Phase 6:
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
 * `useReviewJobsForTicket` / `useHitlHistory` polling. SSE invalidation
 * is a Phase 6 polish item.
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
import { useState } from "react";
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

const DEFAULT_STATUS_META: StatusMeta = {
  label: "Running",
  icon: Loader2,
  chip: "bg-info/15 text-info border-info/30",
};

const M06_STATUS_META: Record<string, StatusMeta> = {
  running: DEFAULT_STATUS_META,
  hitl: { label: "HITL", icon: Bell, chip: "bg-warning/15 text-warning border-warning/30" },
  done: {
    label: "Done",
    icon: CheckCircle2,
    chip: "bg-success/15 text-success border-success/30",
  },
  failed: {
    label: "Failed",
    icon: XCircle,
    chip: "bg-destructive/15 text-destructive border-destructive/30",
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

  const m06Status = ticket.m06_status ?? "running";
  const meta = M06_STATUS_META[m06Status] ?? DEFAULT_STATUS_META;
  const Icon = meta.icon;
  const isTerminal = m06Status === "done" || m06Status === "failed" || m06Status === "cancelled";

  return (
    <div className="mx-auto max-w-[1100px] px-6 py-6" data-testid="ticket-detail">
      <Header
        ticket={ticket}
        m06Status={m06Status}
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
        body="Running again spends LLM tokens. Existing findings persist; the agents look at the latest commit."
        confirmLabel="Re-run"
        pending={rereview.isPending}
        onConfirm={() => {
          rereview.mutate(ticketId, { onSettled: () => setShowRerun(false) });
        }}
      />
    </div>
  );
}

function Header({
  ticket,
  m06Status,
  meta,
  Icon,
  onCancel,
  onRerun,
  isTerminal,
  pendingCancel,
  pendingRerun,
}: {
  ticket: Ticket;
  m06Status: string;
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
            data-testid={`ticket-status-${m06Status}`}
          >
            <Icon className={cn("w-3 h-3", m06Status === "running" && "animate-spin")} />
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
  if (isLoading) return <Skeleton className="h-24" />;
  const events = (jobs ?? []).flatMap((j) => j.activity_log ?? []);
  // Newest events come from the most recent job first; reverse to chronological.
  const ordered = [...events].sort((a, b) => a.ts.localeCompare(b.ts));
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
    <div className="rounded-md border border-border" data-testid="activity-stream">
      {ordered.map((e) => (
        <ActivityEventRow key={`${e.ts}-${e.kind}`} event={e} />
      ))}
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
