/**
 * Derived filter state + filtered/paginated ticket list.
 *
 * Accepts the raw ticket list and the GitHub repos list; derives the
 * repo options and filters the rows so the component is view-only.
 */

import type { GithubRepositoriesResponse, Ticket } from "@core/api";
import { useMemo, useState } from "react";

export type TicketStatus = "running" | "hitl" | "done" | "failed" | "cancelled";

export const ALL_STATUSES: TicketStatus[] = ["running", "hitl", "done", "failed", "cancelled"];

const PAGE_SIZE = 50;

function getTicketStatus(t: Ticket): TicketStatus {
  return t.status;
}

interface UseTicketsFiltersArgs {
  tickets: Ticket[];
  repos: GithubRepositoriesResponse | undefined;
  myEmail: string | null | undefined;
}

export function useTicketsFilters({ tickets, repos, myEmail }: UseTicketsFiltersArgs) {
  const [activeStatuses, setActiveStatuses] = useState<Set<TicketStatus>>(new Set(ALL_STATUSES));
  const [repo, setRepo] = useState<string>("all");
  const [query, setQuery] = useState<string>("");
  const [myOnly, setMyOnly] = useState(false);
  const [visible, setVisible] = useState(PAGE_SIZE);

  const repoOptions = useMemo(() => {
    const fromInstall = new Set((repos?.repositories ?? []).map((r) => r.full_name));
    const fromTickets = new Set(tickets.map((t) => t.repo_external_id).filter(Boolean));
    return Array.from(new Set([...fromInstall, ...fromTickets])).sort();
  }, [repos, tickets]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tickets.filter((t) => {
      if (!activeStatuses.has(getTicketStatus(t))) return false;
      if (repo !== "all" && t.repo_external_id !== repo) return false;
      if (q && !t.title.toLowerCase().includes(q)) return false;
      if (myOnly && myEmail && t.author_login !== myEmail) return false;
      return true;
    });
  }, [tickets, activeStatuses, repo, query, myOnly, myEmail]);

  const pageRows = filtered.slice(0, visible);
  const hasMore = filtered.length > pageRows.length;

  const hasFilters =
    activeStatuses.size !== ALL_STATUSES.length || repo !== "all" || query.length > 0 || myOnly;

  const toggleStatus = (s: TicketStatus) => {
    setActiveStatuses((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  };

  return {
    activeStatuses,
    toggleStatus,
    repo,
    setRepo,
    query,
    setQuery,
    myOnly,
    setMyOnly,
    repoOptions,
    filtered,
    pageRows,
    hasMore,
    hasFilters,
    loadMore: () => setVisible((v) => v + PAGE_SIZE),
  };
}
