/**
 * Tickets list — M06 anchor page (E2a.1).
 *
 * One row per ticket. M06 status vocab in the badge (running / hitl / done /
 * failed / cancelled). Filter bar: status multi-select chips, repo single-
 * select, free-text search over title, "My tickets" toggle. Load-more
 * pagination (no infinite scroll). Row click → Ticket detail.
 *
 * State patterns per C2: skeleton on first load, EmptyState on zero-result,
 * filtered-empty when filters are applied, ErrorBanner on fetch failure.
 *
 * Per requirements.md § F1 the column source-of-truth is the backend's
 * extended `/api/tickets` response shape (`{items, next_cursor}` —
 * `next_cursor` is null in M06; we use naive limit pagination). See
 * `useTickets()` in `apps/web/src/core/api/queries.ts`.
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
import { useMemo, useState } from "react";

type M06Status = "running" | "hitl" | "done" | "failed" | "cancelled";

// Solid semantic-color chips pass WCAG AA contrast against the matching
// `*-foreground` pair. The previous /15-tinted variant failed axe scans
// because the same-color text on the same-color tint sat below the 4.5:1
// contrast ratio.
const STATUS_DISPLAY: Record<M06Status, { label: string; icon: typeof Loader2; chip: string }> = {
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

const ALL_STATUSES: M06Status[] = ["running", "hitl", "done", "failed", "cancelled"];

const PAGE_SIZE = 50;

function getM06Status(t: Ticket): M06Status {
  return t.status;
}

export function TicketsListPage() {
  const { data: ticketsResp, isLoading, isError, error, refetch } = useTickets();
  const { data: repos } = useGithubRepositories();
  const { data: user } = useCurrentUser();
  const [activeStatuses, setActiveStatuses] = useState<Set<M06Status>>(
    new Set(["running", "hitl"]),
  );
  const [repo, setRepo] = useState<string>("all");
  const [query, setQuery] = useState<string>("");
  const [myOnly, setMyOnly] = useState(false);
  const [visible, setVisible] = useState(PAGE_SIZE);

  const repoOptions = useMemo(() => {
    const fromInstall = new Set((repos?.repositories ?? []).map((r) => r.full_name));
    const fromTickets = new Set((ticketsResp ?? []).map((t) => t.repo_external_id).filter(Boolean));
    return Array.from(new Set([...fromInstall, ...fromTickets])).sort();
  }, [repos, ticketsResp]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (ticketsResp ?? []).filter((t) => {
      if (!activeStatuses.has(getM06Status(t))) return false;
      if (repo !== "all" && t.repo_external_id !== repo) return false;
      if (q && !t.title.toLowerCase().includes(q)) return false;
      if (myOnly && user?.user.primary_email && t.author_login !== user.user.primary_email) {
        return false;
      }
      return true;
    });
  }, [ticketsResp, activeStatuses, repo, query, myOnly, user]);

  const totalLoaded = ticketsResp?.length ?? 0;
  const pageRows = filtered.slice(0, visible);
  const hasMore = filtered.length > pageRows.length;

  const toggleStatus = (s: M06Status) => {
    setActiveStatuses((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  };

  const hasFilters =
    activeStatuses.size !== 2 ||
    !activeStatuses.has("running") ||
    !activeStatuses.has("hitl") ||
    repo !== "all" ||
    query.length > 0 ||
    myOnly;

  return (
    <div className="mx-auto max-w-[1280px] px-6 py-6">
      <PageHeader
        title="Tickets"
        subtitle={`${totalLoaded} ${totalLoaded === 1 ? "ticket" : "tickets"} loaded.`}
      />

      <div className="flex flex-wrap items-center gap-2 mb-4">
        {ALL_STATUSES.map((s) => {
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

      {isError && (
        <ErrorBanner
          message={(error as Error).message || "Couldn't load tickets."}
          onRetry={() => refetch()}
          className="mb-4"
        />
      )}

      {isLoading ? (
        <TableSkeleton />
      ) : pageRows.length === 0 ? (
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
              <Button
                variant="outline"
                onClick={() => setVisible((v) => v + PAGE_SIZE)}
                data-testid="tickets-load-more"
              >
                Load more
              </Button>
            </div>
          )}
        </>
      )}
    </div>
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
  const m06 = getM06Status(ticket);
  const meta = STATUS_DISPLAY[m06];
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
          <Icon className={cn("w-3 h-3", m06 === "running" && "animate-spin")} />
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
