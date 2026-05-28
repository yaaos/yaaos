/**
 * Dashboard — anchor page (E2a.3).
 *
 * Two states:
 *   - Configured: stat cards (4) + "In flight" band + "Needs attention" band.
 *   - Not configured: setup banner (`NotConfiguredBanner`) renders above
 *     stat cards; bands are still shown but typically empty.
 *
 * Single round-trip via `useDashboard()` → GET /api/tickets/dashboard.
 * `refetchInterval: 5_000` covers SSE gaps; invalidation wiring
 * (workflow_state_changed → invalidate) lands once dashboard kinds emit.
 */

import { type DashboardStats, type Ticket, useConfigStatus, useDashboard } from "@core/api";
import { EmptyState, NotConfiguredBanner, PageHeader } from "@shared/components/layout";
import { Skeleton } from "@shared/components/ui/skeleton";
import { ago } from "@shared/utils/ago";
import { cn } from "@shared/utils/cn";
import { Link } from "@tanstack/react-router";
import { AlertCircle, Bell, CheckCircle2, Loader2, XCircle } from "lucide-react";

export function DashboardPage() {
  const { data: dashboard, isLoading } = useDashboard();
  const { data: configStatus } = useConfigStatus();

  if (isLoading || !dashboard) {
    return (
      <div className="mx-auto max-w-[1200px] px-6 py-6" data-testid="dashboard-loading">
        <PageHeader title="Dashboard" />
        <div className="grid grid-cols-4 gap-3 mb-6">
          {Array.from({ length: 4 }).map((_, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: skeletons
            <Skeleton key={i} className="h-20" />
          ))}
        </div>
        <Skeleton className="h-32 mb-4" />
        <Skeleton className="h-32" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[1200px] px-6 py-6" data-testid="dashboard-populated">
      <PageHeader title="Dashboard" subtitle="What yaaos is working on right now." />

      {configStatus && !configStatus.configured && <NotConfiguredBanner className="mb-4" />}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
        <StatCard
          label="In flight"
          value={dashboard.stats.in_flight}
          icon={Loader2}
          tone="info"
          spin={dashboard.stats.in_flight > 0}
        />
        <StatCard
          label="HITL pending"
          value={dashboard.stats.hitl_pending}
          icon={Bell}
          tone="warning"
        />
        <StatCard
          label="Completed today"
          value={dashboard.stats.completed_today}
          icon={CheckCircle2}
          tone="success"
        />
        <StatCard
          label="Failed today"
          value={dashboard.stats.failed_today}
          icon={XCircle}
          tone="destructive"
        />
      </div>

      <section className="mb-8">
        <BandHeader title="In flight" count={dashboard.in_flight.length} />
        {dashboard.in_flight.length === 0 ? (
          <EmptyState
            icon={Loader2}
            headline="Nothing in flight."
            body="When yaaos picks up a PR for review, it shows up here."
          />
        ) : (
          <RowList>
            {dashboard.in_flight.map((t) => (
              <InFlightRow key={t.id} ticket={t} />
            ))}
          </RowList>
        )}
      </section>

      <section>
        <BandHeader title="Needs attention" count={dashboard.needs_attention.length} />
        {dashboard.needs_attention.length === 0 ? (
          <EmptyState
            icon={AlertCircle}
            headline="No tickets need attention."
            body="High-severity findings on completed reviews show up here for a Builder to ack or push back."
          />
        ) : (
          <RowList>
            {dashboard.needs_attention.map((t) => (
              <NeedsAttentionRow key={t.id} ticket={t} />
            ))}
          </RowList>
        )}
      </section>
    </div>
  );
}

interface StatCardProps {
  label: string;
  value: number;
  icon: typeof Loader2;
  tone: "info" | "warning" | "success" | "destructive";
  spin?: boolean;
}

function StatCard({ label, value, icon: Icon, tone, spin }: StatCardProps) {
  const toneClass = {
    info: "text-info",
    warning: "text-warning",
    success: "text-success",
    destructive: "text-destructive",
  }[tone];
  return (
    <div className="rounded-md border border-border bg-card p-4 flex items-start gap-3">
      <Icon className={cn("w-5 h-5 mt-0.5", toneClass, spin && "animate-spin")} />
      <div className="flex-1 min-w-0">
        <div className="text-xs text-muted-foreground font-medium uppercase tracking-wider">
          {label}
        </div>
        <div className="text-2xl font-semibold mt-1">{value}</div>
      </div>
    </div>
  );
}

function BandHeader({ title, count }: { title: string; count: number }) {
  return (
    <div className="flex items-baseline justify-between mb-3">
      <div className="flex items-baseline gap-2">
        <h2 className="text-lg font-medium">{title}</h2>
        <span className="text-xs text-muted-foreground">{count}</span>
      </div>
      {count > 0 && (
        <Link
          to="/orgs/$slug/tickets"
          params={(prev) => ({ slug: prev.slug as string })}
          className="text-xs text-muted-foreground hover:text-foreground transition-colors"
        >
          View all
        </Link>
      )}
    </div>
  );
}

function RowList({ children }: { children: React.ReactNode }) {
  return <div className="rounded-md border border-border overflow-hidden">{children}</div>;
}

function InFlightRow({ ticket }: { ticket: Ticket }) {
  return (
    <Link
      to="/orgs/$slug/tickets/$ticketId"
      params={(prev) => ({ slug: prev.slug as string, ticketId: ticket.id })}
      className="flex items-center gap-3 px-3 py-2.5 border-b border-border last:border-0 hover:bg-accent text-sm transition-colors"
      data-testid={`dashboard-inflight-${ticket.id}`}
    >
      <Loader2 className="w-4 h-4 shrink-0 text-info animate-spin" />
      <span className="flex-1 truncate font-medium">{ticket.title}</span>
      <span className="text-xs text-muted-foreground mono shrink-0">{ticket.repo_external_id}</span>
      <span className="text-xs text-muted-foreground shrink-0">{ago(ticket.updated_at)}</span>
    </Link>
  );
}

function NeedsAttentionRow({ ticket }: { ticket: Ticket }) {
  const severityClass =
    ticket.max_severity === "high"
      ? "text-destructive"
      : ticket.max_severity === "medium"
        ? "text-warning"
        : "text-info";
  return (
    <Link
      to="/orgs/$slug/tickets/$ticketId"
      params={(prev) => ({ slug: prev.slug as string, ticketId: ticket.id })}
      className="flex items-center gap-3 px-3 py-2.5 border-b border-border last:border-0 hover:bg-accent text-sm transition-colors"
      data-testid={`dashboard-needs-attention-${ticket.id}`}
    >
      <AlertCircle className={cn("w-4 h-4 shrink-0", severityClass)} />
      <span className="flex-1 truncate font-medium">{ticket.title}</span>
      <span className="text-xs">
        {ticket.findings_count} {ticket.findings_count === 1 ? "finding" : "findings"}
      </span>
      <span className="text-xs text-muted-foreground mono shrink-0">{ticket.repo_external_id}</span>
    </Link>
  );
}

// Re-export so dashboard.test.tsx's import keeps resolving;
// the test mocks `useOnboarding` directly.
export type { DashboardStats };
