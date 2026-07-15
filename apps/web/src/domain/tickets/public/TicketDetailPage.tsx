/**
 * Ticket detail — three in-content tabs: Overview (attention block / live
 * card / outcome card, branching on the current run's server-computed
 * status), Runs (timeline of every run + its stage executions), and
 * Artifacts (versioned rendered markdown per stage).
 *
 * Sections:
 *   1. Header band — title + status pill.
 *   2. Tab strip — Overview / Runs / Artifacts.
 *   3. Tab body.
 *
 * Live updates: `run_state_changed` / `stage_state_changed` / `artifact_stored`
 * SSE invalidate `["runs", ticketId]`, `["runs","overview",ticketId]`, and
 * `["artifacts", ticketId]` respectively — see `core/sse`.
 */

import type { Ticket } from "@core/api/public/client";
import { useTicket } from "@core/api/public/queries";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { Skeleton } from "@shared/components/ui/skeleton";
import { ago } from "@shared/utils/public/ago";
import { cn } from "@shared/utils/public/cn";
import { useParams } from "@tanstack/react-router";
import { Bell, CheckCircle2, CircleDashed, Loader2, XCircle } from "lucide-react";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { ArtifactsTab } from "../artifacts";
import { OverviewTab } from "../overview";
import { RunsTab } from "../runs";

type Tab = "overview" | "runs" | "artifacts";

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
  const [tab, setTab] = useState<Tab>("overview");

  const status = ticket.status;
  const meta = STATUS_META[status] ?? DEFAULT_STATUS_META;
  const Icon = meta.icon;

  return (
    <div data-testid="ticket-detail">
      <Header ticket={ticket} status={status} meta={meta} Icon={Icon} />

      <Tabs tab={tab} onChange={setTab} />

      <div className="mt-4">
        {tab === "overview" && (
          <div data-testid="ticket-overview">
            <OverviewTab
              ticketId={ticketId}
              ticketType={ticket.type}
              onShowRuns={() => setTab("runs")}
            />
          </div>
        )}
        {tab === "runs" && (
          <ErrorBoundary
            fallbackRender={({ resetErrorBoundary }) => (
              <ErrorBanner message="Couldn't load runs." onRetry={resetErrorBoundary} />
            )}
          >
            <Suspense fallback={<Skeleton className="h-48" />}>
              <div data-testid="ticket-runs">
                <RunsTab ticketId={ticketId} />
              </div>
            </Suspense>
          </ErrorBoundary>
        )}
        {tab === "artifacts" && (
          <ErrorBoundary
            fallbackRender={({ resetErrorBoundary }) => (
              <ErrorBanner message="Couldn't load artifacts." onRetry={resetErrorBoundary} />
            )}
          >
            <Suspense fallback={<Skeleton className="h-48" />}>
              <div data-testid="ticket-artifacts">
                <ArtifactsTab ticketId={ticketId} />
              </div>
            </Suspense>
          </ErrorBoundary>
        )}
      </div>
    </div>
  );
}

function Header({
  ticket,
  status,
  meta,
  Icon,
}: {
  ticket: Ticket;
  status: string;
  meta: { label: string; chip: string };
  Icon: typeof Loader2;
}) {
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
            by {ticket.builder_kind === "system" ? "yaaos" : (ticket.builder_display_name ?? "—")}
          </span>
        </div>
      </div>
    </header>
  );
}

function Tabs({ tab, onChange }: { tab: Tab; onChange: (t: Tab) => void }) {
  const items: Array<{ id: Tab; label: string }> = [
    { id: "overview", label: "Overview" },
    { id: "runs", label: "Runs" },
    { id: "artifacts", label: "Artifacts" },
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
