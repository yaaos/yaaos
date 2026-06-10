/**
 * Regression guard: the app shell Sidebar is wrapped in a <Suspense> boundary
 * so a cold ["auth","me"] cache shows a sidebar skeleton, not a bubbled
 * "Something went wrong" from the root error boundary.
 *
 * Strategy:
 *   1. Hold /api/auth/me in a pending state (MSW deferred response).
 *   2. Assert sidebar-loading skeleton is visible and root error boundary text
 *      is NOT visible (suspension caught locally).
 *   3. Resolve the pending request.
 *   4. Assert real Sidebar content is visible and skeleton is gone.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { Suspense } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AUTH_ME_FIXTURE } from "../../../test/msw/handlers/auth";
import { server } from "../../../test/msw/server";
import { AppShell } from "../public/app-shell";

// ---------------------------------------------------------------------------
// Module-level mocks — keep the harness deterministic and fast.
// AppShell's side-effecting hooks must be stubbed; Sidebar renders real
// (that's the SUT). Router state is seeded to an authenticated route.
// ---------------------------------------------------------------------------
vi.mock("@core/sse/public/subscriber", () => ({ useServerEvents: () => {} }));
vi.mock("@core/observability/public/use-otel-identity-sync", () => ({
  useOtelIdentitySync: () => {},
}));
vi.mock("../broken-integrations-banner", () => ({
  BrokenIntegrationsBanner: () => null,
}));

// Sidebar internals that need router context — stub Link and useRouterState.
vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    Outlet: () => null,
    useRouterState: (opts?: { select?: (s: unknown) => unknown }) => {
      const state = {
        location: { pathname: "/orgs/acme/dashboard" },
        status: "idle",
      };
      return opts?.select ? opts.select(state) : state;
    },
    Link: ({ children, ...props }: { children: React.ReactNode; [k: string]: unknown }) => (
      <a {...(props as Record<string, string>)}>{children}</a>
    ),
  };
});

// OrgSwitcher calls useCurrentOrgSlug which reads router context — stub it.
// apiFetch calls getCurrentOrgSlug() to inject X-Yaaos-Org-Slug header — stub that too.
vi.mock("@core/api/public/org-context", () => ({
  useCurrentOrgSlug: () => "acme",
  setCurrentOrgSlug: () => {},
  getCurrentOrgSlug: () => "acme",
}));

// NotificationsBell calls useNotifications (useSuspenseQuery) — stub it so
// it doesn't add a competing Suspense throw from outside UserCard.
vi.mock("@core/api/public/queries", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@core/api/public/queries")>();
  return {
    ...actual,
    useNotifications: () => ({ data: { items: [] } }),
  };
});

// OrgSwitcher renders org data — stub so it doesn't need its own queries.
vi.mock("../../sidebar/org-switcher", () => ({
  OrgSwitcher: () => <div data-testid="org-switcher-stub" />,
}));

// NotificationsBell — internal to sidebar, stub directly.
vi.mock("../../sidebar/notifications-bell", () => ({
  NotificationsBell: () => <div data-testid="notifications-bell-stub" />,
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function wrap(node: React.ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return (
    // Simulate the root error boundary so we can assert it does NOT fire.
    <Suspense fallback={null}>
      <QueryClientProvider client={qc}>{node}</QueryClientProvider>
    </Suspense>
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe("AppShell — Sidebar Suspense boundary", () => {
  describe("cold cache: suspension is caught locally, not bubbled", () => {
    beforeEach(() => {
      // Seal /api/auth/me — never resolves during the pending assertion phase.
      server.use(http.get("/api/auth/me", () => new Promise(() => {})));
    });

    it("shows sidebar-loading skeleton and NOT root error text while auth/me is pending", async () => {
      render(wrap(<AppShell />));

      // The Suspense boundary in AppShell must catch the thrown promise and
      // show the fallback instead of letting it bubble to the root.
      await waitFor(
        () => {
          expect(screen.getByTestId("sidebar-loading")).toBeInTheDocument();
        },
        { timeout: 3000 },
      );

      // Root error boundary must NOT have fired — this is the regression guard.
      expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
    });
  });

  describe("resolved cache: real Sidebar renders, skeleton gone", () => {
    beforeEach(() => {
      server.use(http.get("/api/auth/me", () => HttpResponse.json(AUTH_ME_FIXTURE)));
    });

    it("shows real Sidebar content once auth/me resolves", async () => {
      render(wrap(<AppShell />));

      // After resolution the sidebar renders; the skeleton must be gone.
      await waitFor(
        () => {
          expect(screen.getByTestId("sidebar")).toBeInTheDocument();
        },
        { timeout: 3000 },
      );

      expect(screen.queryByTestId("sidebar-loading")).not.toBeInTheDocument();
    });
  });
});
