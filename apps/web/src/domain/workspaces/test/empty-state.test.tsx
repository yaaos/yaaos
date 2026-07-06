/**
 * WorkspacesContent — empty-state branches.
 *
 * Two branches, both rendered when `agents.length === 0`:
 *   - configured  → EmptyState + CTA linking to `/org/$slug/settings/workspaces`.
 *   - unconfigured → NotConfiguredBanner.
 *
 * Component-tier test per apps/web/docs/patterns.md (Vitest + RTL + MSW):
 * real QueryClient + real apiFetch, HTTP intercepted at the MSW boundary.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "../../../test/msw/server";
import { WorkspacesPage } from "../public";

// Link from @tanstack/react-router: no router context in these tests, so the
// stub renders an <a> that surfaces the `to` string as an attribute. The test
// asserts the destination string — router-side param interpolation is
// TanStack's concern, not ours.
vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, to, ...rest }: { children: React.ReactNode; to: string }) => (
    <a data-to={to} href={to} {...(rest as Record<string, unknown>)}>
      {children}
    </a>
  ),
  useRouterState: ({ select }: { select: (s: { location: { pathname: string } }) => unknown }) =>
    select({ location: { pathname: window.location.pathname } }),
}));

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

function seedAuthMe() {
  server.use(
    http.get("/api/auth/me", () =>
      HttpResponse.json({
        user: { id: "u1", display_name: "Owner", primary_email: "o@x.test", emails: [] },
        memberships: [
          {
            org_id: "o1",
            slug: "acme-empty",
            display_name: "Acme",
            role: "owner",
            handle: "owner",
          },
        ],
      }),
    ),
  );
}

describe("WorkspacesPage — empty-state branches", () => {
  beforeEach(() => {
    // Set the URL so getCurrentOrgSlug() resolves to "acme-empty".
    window.history.pushState({}, "", "/org/acme-empty/workspaces");
  });

  afterEach(() => {
    window.history.pushState({}, "", "/");
  });

  it("renders EmptyState with CTA to /settings/workspaces when configured and zero agents", async () => {
    seedAuthMe();
    server.use(
      http.get("/api/orgs/acme-empty/agents", () => HttpResponse.json([])),
      http.get("/api/orgs/config-status", () =>
        HttpResponse.json({ configured: true, missing: [], admins: [] }),
      ),
    );

    render(wrap(<WorkspacesPage />));

    const empty = await screen.findByTestId("workspaces-empty");
    expect(empty).toBeInTheDocument();

    const cta = empty.querySelector("a");
    expect(cta).not.toBeNull();
    expect(cta).toHaveAttribute("data-to", "/org/$slug/settings/workspaces");
  });

  it("renders NotConfiguredBanner when unconfigured and zero agents", async () => {
    seedAuthMe();
    server.use(
      http.get("/api/orgs/acme-empty/agents", () => HttpResponse.json([])),
      http.get("/api/orgs/config-status", () =>
        HttpResponse.json({
          configured: false,
          missing: ["vcs", "coding_agent", "api_key", "workspace"],
          admins: [],
        }),
      ),
    );

    render(wrap(<WorkspacesPage />));

    await waitFor(() => expect(screen.queryByTestId("workspaces-empty")).not.toBeInTheDocument());
    // NotConfiguredBanner renders — its copy is stable enough to key on.
    expect(await screen.findByText(/yaaos is not fully configured/i)).toBeInTheDocument();
  });
});
