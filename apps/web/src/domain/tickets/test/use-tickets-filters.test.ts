/**
 * Unit tests for useTicketsFilters. Pure logic: no network, no React render.
 */

import type { Ticket } from "@core/api/public/client";
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ALL_STATUSES, useTicketsFilters } from "../use-tickets-filters";

function makeTicket(overrides: Partial<Ticket> = {}): Ticket {
  return {
    id: "t1",
    org_id: "o1",
    source: "github_pr",
    source_external_id: "x/y#1",
    title: "Fix bug",
    description: null,
    status: "running",
    type: "pr_review",
    plugin_id: "github",
    repo_external_id: "acme/api",
    pr_id: "p1",
    pr_number: 1,
    pr_html_url: null,
    author_login: "alice",
    is_draft: false,
    created_at: "2026-05-23T00:00:00Z",
    updated_at: "2026-05-23T00:00:00Z",
    current_stage: "Review",
    findings_count: 0,
    max_severity: null,
    builder_kind: "user",
    builder_display_name: "alice",
    builder: { kind: "user", display_name: "alice" },
    ...overrides,
  };
}

const tickets: Ticket[] = [
  makeTicket({
    id: "t1",
    status: "running",
    repo_external_id: "acme/api",
    author_login: "alice",
    title: "Add endpoint",
  }),
  makeTicket({
    id: "t2",
    status: "done",
    repo_external_id: "acme/web",
    author_login: "bob",
    title: "Fix styles",
  }),
  makeTicket({
    id: "t3",
    status: "failed",
    repo_external_id: "acme/api",
    author_login: "alice",
    title: "Add logging",
  }),
];

describe("useTicketsFilters", () => {
  it("returns all tickets when no filters are active", () => {
    const { result } = renderHook(() =>
      useTicketsFilters({ tickets, repos: undefined, myEmail: null }),
    );
    expect(result.current.filtered.length).toBe(3);
    expect(result.current.hasFilters).toBe(false);
  });

  it("toggleStatus removes a status from the active set", () => {
    const { result } = renderHook(() =>
      useTicketsFilters({ tickets, repos: undefined, myEmail: null }),
    );
    act(() => {
      result.current.toggleStatus("done");
    });
    // "done" removed → only running and failed remain from our fixture
    expect(result.current.filtered.some((t) => t.status === "done")).toBe(false);
    expect(result.current.hasFilters).toBe(true);
  });

  it("filters by repo", () => {
    const { result } = renderHook(() =>
      useTicketsFilters({ tickets, repos: undefined, myEmail: null }),
    );
    act(() => {
      result.current.setRepo("acme/api");
    });
    expect(result.current.filtered.every((t) => t.repo_external_id === "acme/api")).toBe(true);
    expect(result.current.filtered.length).toBe(2);
  });

  it("filters by search query (case-insensitive substring)", () => {
    const { result } = renderHook(() =>
      useTicketsFilters({ tickets, repos: undefined, myEmail: null }),
    );
    act(() => {
      result.current.setQuery("FIX");
    });
    expect(result.current.filtered.length).toBe(1);
    expect(result.current.filtered[0]?.id).toBe("t2");
  });

  it("myOnly filters to tickets authored by the current user's email", () => {
    const { result } = renderHook(() =>
      useTicketsFilters({ tickets, repos: undefined, myEmail: "alice" }),
    );
    act(() => {
      result.current.setMyOnly(true);
    });
    expect(result.current.filtered.every((t) => t.author_login === "alice")).toBe(true);
    expect(result.current.filtered.length).toBe(2);
  });

  it("hasMore is true when filtered list exceeds visible count (PAGE_SIZE=50)", () => {
    // Create 51 tickets to exceed PAGE_SIZE.
    const manyTickets = Array.from({ length: 51 }, (_, i) =>
      makeTicket({ id: `t${i}`, title: `Ticket ${i}` }),
    );
    const { result } = renderHook(() =>
      useTicketsFilters({ tickets: manyTickets, repos: undefined, myEmail: null }),
    );
    expect(result.current.hasMore).toBe(true);
    expect(result.current.pageRows.length).toBe(50);
    act(() => {
      result.current.loadMore();
    });
    expect(result.current.pageRows.length).toBe(51);
    expect(result.current.hasMore).toBe(false);
  });

  it("repoOptions merges repos from installation list and from tickets", () => {
    const { result } = renderHook(() =>
      useTicketsFilters({
        tickets,
        repos: {
          total_count: 1,
          repositories: [{ full_name: "acme/infra", html_url: "https://x", private: false }],
        },
        myEmail: null,
      }),
    );
    expect(result.current.repoOptions).toContain("acme/api");
    expect(result.current.repoOptions).toContain("acme/web");
    expect(result.current.repoOptions).toContain("acme/infra");
  });

  it("ALL_STATUSES matches the exported constant", () => {
    expect(ALL_STATUSES).toEqual(["pending", "running", "hitl", "done", "failed", "cancelled"]);
  });
});
