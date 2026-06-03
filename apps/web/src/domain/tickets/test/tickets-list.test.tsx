import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { describe, expect, it, vi } from "vitest";
import { server } from "../../../test/msw/server";
import { TicketsListPage } from "../TicketsListPage";

/**
 * Smoke tests for the Tickets list page. Uses MSW to intercept:
 *   - GET /api/tickets — the ticket list.
 *   - GET /api/github/repositories — repo list for the filter.
 *   - GET /api/auth/me — current user (for "My tickets" toggle).
 *
 * Asserts:
 *   1. The filter chips render with status vocab labels.
 *   2. The empty-state branch renders when the API returns zero rows.
 */

// Link from @tanstack/react-router needs stubbing (no router context in tests).
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, ...props }: { children: React.ReactNode; [k: string]: unknown }) => (
    <a {...(props as Record<string, string>)}>{children}</a>
  ),
}));

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

describe("TicketsListPage (MSW)", () => {
  it("renders the status chips after data loads", async () => {
    server.use(
      http.get("/api/tickets", () => HttpResponse.json({ items: [], next_cursor: null })),
      http.get("/api/github/repositories", () =>
        HttpResponse.json({ total_count: 0, repositories: [] }),
      ),
      http.get("/api/auth/me", () =>
        HttpResponse.json({
          user: { id: "u1", display_name: "Jane", primary_email: "j@x.test", emails: [] },
          memberships: [
            { org_id: "o1", slug: "acme", display_name: "Acme", role: "admin", handle: "jane" },
          ],
        }),
      ),
    );
    render(wrap(<TicketsListPage />));
    // The filter chips are rendered by TicketsList (inside Suspense).
    // Wait for the data to resolve and the chips to appear.
    await waitFor(() => {
      for (const s of ["running", "hitl", "done", "failed", "cancelled"]) {
        expect(screen.getByTestId(`tickets-filter-${s}`)).toBeInTheDocument();
      }
    });
  });

  it("renders the empty state when there are zero tickets", async () => {
    server.use(
      http.get("/api/tickets", () => HttpResponse.json({ items: [], next_cursor: null })),
      http.get("/api/github/repositories", () =>
        HttpResponse.json({ total_count: 0, repositories: [] }),
      ),
      http.get("/api/auth/me", () =>
        HttpResponse.json({
          user: { id: "u1", display_name: "Jane", primary_email: "j@x.test", emails: [] },
          memberships: [],
        }),
      ),
    );
    render(wrap(<TicketsListPage />));
    await waitFor(() => expect(screen.getByText(/No tickets yet/i)).toBeInTheDocument());
  });
});
