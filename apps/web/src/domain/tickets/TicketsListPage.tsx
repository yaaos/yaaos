/**
 * Tickets list page.
 *
 * One row per ticket. Five-state status vocab in the badge (running / hitl /
 * done / failed / cancelled). Filter bar: status multi-select chips, repo
 * single-select, free-text search over title, "My tickets" toggle. Load-more
 * pagination (no infinite scroll). Row click → Ticket detail.
 *
 * State patterns: Suspense skeleton on first load (ErrorBoundary catches
 * fetch failures), EmptyState on zero-result, filtered-empty when filters
 * are applied.
 *
 * The column source-of-truth is the backend's `/api/tickets` response
 * (`{items, next_cursor}`; cursor is null today — naive limit pagination).
 * See `useTickets()` in `apps/web/src/core/api/queries.ts`.
 */

import { type Ticket, useGithubRepositories, useTickets } from "@core/api";
import { useCurrentUser } from "@domain/auth";
import { EmptyState, ErrorBanner, PageHeader } from "@shared/components/layout";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Input } from "@shared/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@shared/components/ui/select";
import { Skeleton } from "@shared/components/ui/skeleton";
import { ago } from "@shared/utils/ago";
import { cn } from "@shared/utils/cn";
import { Link } from "@tanstack/react-router";
import {
  CheckCircle2,
  CircleDashed,
  Hand,
  Loader2,
  Search,
  Ticket as TicketIcon,
  XCircle,
} from "lucide-react";
import { Suspense } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { type TicketStatus, useTicketsFilters } from "./use-tickets-filters";

// Solid semantic-color chips pass WCAG AA contrast against the matching
// `*-foreground` pair. The previous /15-tinted variant failed axe scans
// because the same-color text on the same-color tint sat below the 4.5:1
// contrast ratio.
const STATUS_DISPLAY: Record<TicketStatus, { label: string; icon: typeof Loader2; chip: string }> =
  {
    running: { label: "Running", icon: Loader2, chip: "bg-info text-info-foreground border-info" },
    hitl: {
      label: "HITL",
      icon: Hand,
      chip: "bg-warning text-warning-foreground border-warning",
    },
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

const ALL_STATUSES_DISPLAY: TicketStatus[] = ["running", "hitl", "done", "failed", "cancelled"];

export function TicketsListPage() {
  return (
    <div className="mx-auto max-w-[1280px] px-6 py-6">
      <PageHeader title="Tickets" />
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner
            message="Couldn't load tickets."
            onRetry={resetErrorBoundary}
            className="mb-4"
          />
        )}
      >
        <Suspense fallback={<TableSkeleton />}>
          <TicketsList />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}

function TicketsList() {
  const { data: tickets } = useTickets();
  const { data: repos } = useGithubRepositories();
  const { data: user } = useCurrentUser();

  const myEmail = user?.user.primary_email;

  const {
    activeStatuses,
    toggleStatus,
    repo,
    setRepo,
    query,
    setQuery,
    myOnly,
    setMyOnly,
    repoOptions,
    pageRows,
    hasMore,
    hasFilters,
    loadMore,
  } = useTicketsFilters({ tickets: tickets ?? [], repos, myEmail });

  const totalLoaded = tickets?.length ?? 0;

  return (
    <>
      <div className="mb-2 text-xs text-muted-foreground">
        {`${totalLoaded} ${totalLoaded === 1 ? "ticket" : "tickets"} loaded.`}
      </div>
      <div className="flex flex-wrap items-center gap-2 mb-4">
        {ALL_STATUSES_DISPLAY.map((s) => {
          const meta = STATUS_DISPLAY[s];
          const Icon = meta.icon;
          const active = activeStatuses.has(s);
          return (
            <button
              key={s}
              type="button"
              onClick={() => toggleStatus(s)}
              data-testid={`tickets-filter-${s}`}
              aria-pressed={active}
              className={cn(
                "inline-flex items-center gap-1.5 h-7 px-2.5 rounded-full text-xs font-medium border transition-colors",
                active
                  ? meta.chip
                  : "bg-secondary text-muted-foreground border-border hover:text-foreground",
              )}
            >
              <Icon className="w-3 h-3" />
              {meta.label}
            </button>
          );
        })}

        <div className="w-2" />

        <div className="relative">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
          <Input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search title…"
            data-testid="tickets-search"
            aria-label="Search tickets by title"
            className="h-7 pl-7 text-xs w-[200px]"
          />
        </div>

        <Select value={repo} onValueChange={setRepo}>
          <SelectTrigger
            className="h-7 text-xs w-[160px]"
            data-testid="tickets-filter-repo"
            aria-label="Filter by repository"
          >
            <SelectValue placeholder="All repos" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All repos</SelectItem>
            {repoOptions.map((r) => (
              <SelectItem key={r} value={r}>
                {r}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <button
          type="button"
          onClick={() => setMyOnly((v) => !v)}
          aria-pressed={myOnly}
          data-testid="tickets-filter-mine"
          className={cn(
            "h-7 px-2.5 rounded-full text-xs font-medium border transition-colors",
            myOnly
              ? "bg-primary/10 text-primary border-primary/30"
              : "bg-secondary text-muted-foreground border-border hover:text-foreground",
          )}
        >
          My tickets
        </button>
      </div>

      {pageRows.length === 0 ? (
        hasFilters ? (
          <EmptyState
            icon={Search}
            headline="No tickets match these filters."
            body="Try widening the status set, clearing the search, or switching repos."
          />
        ) : (
          <EmptyState
            icon={TicketIcon}
            headline="No tickets yet."
            body="Tickets appear here after GitHub opens a PR for review."
          />
        )
      ) : (
        <>
          <TicketsTable rows={pageRows} />
          {hasMore && (
            <div className="flex justify-center mt-4">
              <Button variant="outline" onClick={loadMore} data-testid="tickets-load-more">
                Load more
              </Button>
            </div>
          )}
        </>
      )}
    </>
  );
}

function TicketsTable({ rows }: { rows: Ticket[] }) {
  return (
    <div className="border border-border rounded-md overflow-hidden" data-testid="tickets-list">
      <div className="grid grid-cols-[110px_minmax(0,1fr)_180px_90px_90px_110px_140px] items-center gap-3 px-3 h-7 bg-muted/50 border-b border-border text-muted-foreground text-[10.5px] uppercase tracking-wider font-medium">
        <div>Status</div>
        <div>Title</div>
        <div>Repo</div>
        <div>Stage</div>
        <div>Findings</div>
        <div>Updated</div>
        <div>Builder</div>
      </div>
      {rows.map((t) => (
        <TicketRow key={t.id} ticket={t} />
      ))}
    </div>
  );
}

function TicketRow({ ticket }: { ticket: Ticket }) {
  const status = ticket.status as TicketStatus;
  const meta = STATUS_DISPLAY[status] ?? STATUS_DISPLAY.running;
  const Icon = meta.icon;
  const builderName = ticket.builder_display_name ?? ticket.author_login ?? "—";
  const severity = ticket.max_severity;
  return (
    <Link
      to="/orgs/$slug/tickets/$ticketId"
      params={{
        slug: window.location.pathname.split("/")[2] ?? "",
        ticketId: ticket.id,
      }}
      className="grid grid-cols-[110px_minmax(0,1fr)_180px_90px_90px_110px_140px] items-center gap-3 px-3 h-11 border-b border-border last:border-0 hover:bg-accent text-sm transition-colors"
      data-testid={`tickets-row-${ticket.id}`}
    >
      <div>
        <span
          className={cn(
            "inline-flex items-center gap-1.5 h-5 px-1.5 rounded text-[10.5px] font-medium border",
            meta.chip,
          )}
        >
          <Icon className={cn("w-3 h-3", status === "running" && "animate-spin")} />
          {meta.label}
        </span>
      </div>
      <div className="truncate font-medium">{ticket.title}</div>
      <div className="truncate text-xs text-muted-foreground mono">{ticket.repo_external_id}</div>
      <div className="text-xs text-muted-foreground">{ticket.current_stage ?? "Review"}</div>
      <div className="text-xs">
        {ticket.findings_count > 0 ? (
          <span className="inline-flex items-center gap-1">
            {ticket.findings_count}
            {severity && (
              <span
                className={cn(
                  "w-1.5 h-1.5 rounded-full",
                  severity === "high" && "bg-destructive",
                  severity === "medium" && "bg-warning",
                  severity === "low" && "bg-info",
                )}
                title={`Max severity: ${severity}`}
              />
            )}
          </span>
        ) : (
          <span className="text-muted-foreground">0</span>
        )}
      </div>
      <div className="text-xs text-muted-foreground" title={ticket.updated_at}>
        {ago(ticket.updated_at)}
      </div>
      <div className="text-xs truncate">
        {ticket.builder_kind === "system" ? (
          <Badge variant="secondary" className="text-[10.5px]">
            yaaos
          </Badge>
        ) : (
          builderName
        )}
      </div>
    </Link>
  );
}

function TableSkeleton() {
  return (
    <div className="border border-border rounded-md overflow-hidden">
      {Array.from({ length: 6 }).map((_, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: skeleton rows have no identity
        <div key={i} className="px-3 h-11 border-b border-border last:border-0 flex items-center">
          <Skeleton className="h-4 w-full" />
        </div>
      ))}
    </div>
  );
}
