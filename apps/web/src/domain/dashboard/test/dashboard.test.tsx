import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { describe, expect, it, vi } from "vitest";
import { server } from "../../../test/msw/server";
import { DashboardPage } from "../index";

/**
 * Smoke tests for DashboardPage. Uses MSW to intercept the dashboard and
 * related endpoints. The full populated state and agent cards are validated
 * end-to-end; this test covers the Suspense/populated split at the unit tier.
 */

// Link from @tanstack/react-router needs stubbing.
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, ...props }: { children: React.ReactNode; [k: string]: unknown }) => (
    <a {...(props as Record<string, string>)}>{children}</a>
  ),
}));

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

const DASHBOARD_RESPONSE = {
  stats: { in_flight: 0, hitl_pending: 0, completed_today: 0, failed_today: 0 },
  in_flight: [],
  needs_attention: [],
};

describe("DashboardPage (MSW)", () => {
  it("shows Suspense skeleton while loading then renders populated content", async () => {
    server.use(
      http.get("/api/tickets/dashboard", () => HttpResponse.json(DASHBOARD_RESPONSE)),
      http.get("/api/orgs/config-status", () =>
        HttpResponse.json({ configured: true, missing: [], admins: [] }),
      ),
      http.get("/api/orgs/:slug/agents", () => HttpResponse.json([])),
    );
    render(wrap(<DashboardPage />));
    // Suspense skeleton renders first.
    expect(screen.getByTestId("dashboard-loading")).toBeInTheDocument();
    // After data resolves, the populated view renders.
    await waitFor(() => expect(screen.getByTestId("dashboard-populated")).toBeInTheDocument());
  });
});
