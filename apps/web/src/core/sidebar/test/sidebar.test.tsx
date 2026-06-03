import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { Suspense } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "../../../test/msw/server";
import { Sidebar } from "../sidebar";

// useRouterState and Link must be vi.mock'd — they are not HTTP hooks.
const pathnameMock = vi.fn(() => "/orgs/acme/dashboard");

vi.mock("@tanstack/react-router", () => ({
  useRouterState: (opts?: { select?: (s: { location: { pathname: string } }) => unknown }) => {
    const state = { location: { pathname: pathnameMock() } };
    return opts?.select ? opts.select(state) : state;
  },
  Link: ({
    to,
    children,
    ...props
  }: { to: string; children: React.ReactNode } & Record<string, unknown>) => (
    <a href={to} {...props}>
      {children}
    </a>
  ),
}));

function userResp(role: "owner" | "admin" | "builder") {
  return {
    user: {
      id: "u1",
      display_name: "Jane Doe",
      primary_email: "j@x.test",
      emails: [],
    },
    memberships: [{ org_id: "o1", slug: "acme", display_name: "Acme", role, handle: "jane" }],
  };
}

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <Suspense fallback={null}>{node}</Suspense>
    </QueryClientProvider>
  );
}

describe("Sidebar", () => {
  beforeEach(() => {
    localStorage.clear();
    pathnameMock.mockReturnValue("/orgs/acme/dashboard");
    // Default handlers — override per test as needed.
    server.use(
      http.get("/api/auth/me", () => HttpResponse.json(userResp("admin"))),
      http.get("/api/orgs/mine", () =>
        HttpResponse.json([
          { id: "o1", slug: "acme", name: "Acme", role: "admin", last_used_at: null },
        ]),
      ),
      http.get("/api/orgs/config-status", () =>
        HttpResponse.json({ configured: true, missing: [], admins: [] }),
      ),
      http.get("/api/notifications/popover", () =>
        HttpResponse.json({ items: [], unread_count: 0 }),
      ),
      http.post("/api/auth/logout", () => HttpResponse.json({})),
    );
  });

  it("renders top-level org-scoped links + the user card when expanded (snapshot-ish)", async () => {
    // Put the user inside a settings sub-route so the group is expanded
    // and the children render in the DOM for testid lookup.
    pathnameMock.mockReturnValue("/orgs/acme/settings/auth");
    server.use(http.get("/api/auth/me", () => HttpResponse.json(userResp("admin"))));
    render(wrap(<Sidebar />));
    // Sidebar suspends until useCurrentUser resolves; wait for any nav link.
    await waitFor(() =>
      expect(screen.getByTestId("nav-dashboard")).toHaveAttribute("href", "/orgs/acme/dashboard"),
    );
    expect(screen.getByTestId("nav-tickets")).toHaveAttribute("href", "/orgs/acme/tickets");
    expect(screen.getByTestId("nav-lessons")).toHaveAttribute("href", "/orgs/acme/lessons");
    expect(screen.getByTestId("nav-group-org-settings")).toBeInTheDocument();
    // Admin-gated items render once useCurrentUser resolves.
    await waitFor(() => expect(screen.getByTestId("nav-auth")).toBeInTheDocument());
    expect(screen.getByTestId("nav-auth")).toHaveAttribute("data-active");
    expect(screen.getByTestId("nav-members")).toBeInTheDocument();
    expect(screen.getByTestId("nav-vcs")).toBeInTheDocument();
    expect(screen.getByTestId("nav-coding-agents")).toBeInTheDocument();
    expect(screen.getByTestId("nav-api-keys")).toBeInTheDocument();
    expect(screen.getByTestId("nav-audit")).toBeInTheDocument();
  });

  it("members see the Org Settings group but only the Members sub-item", async () => {
    pathnameMock.mockReturnValue("/orgs/acme/settings/members");
    server.use(http.get("/api/auth/me", () => HttpResponse.json(userResp("builder"))));
    render(wrap(<Sidebar />));
    // Sidebar suspends until useCurrentUser resolves; wait for any nav link.
    await waitFor(() => expect(screen.getByTestId("nav-dashboard")).toBeInTheDocument());
    expect(screen.getByTestId("nav-tickets")).toBeInTheDocument();
    expect(screen.getByTestId("nav-group-org-settings")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("nav-members")).toBeInTheDocument());
    expect(screen.queryByTestId("nav-auth")).toBeNull();
    expect(screen.queryByTestId("nav-vcs")).toBeNull();
    expect(screen.queryByTestId("nav-coding-agents")).toBeNull();
    expect(screen.queryByTestId("nav-api-keys")).toBeNull();
    expect(screen.queryByTestId("nav-audit")).toBeNull();
  });

  it("shows admin-gated group items for owners", async () => {
    pathnameMock.mockReturnValue("/orgs/acme/settings/vcs");
    server.use(http.get("/api/auth/me", () => HttpResponse.json(userResp("owner"))));
    render(wrap(<Sidebar />));
    await waitFor(() => expect(screen.getByTestId("nav-group-org-settings")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("nav-vcs")).toBeInTheDocument());
  });

  it("auto-collapses the group when the route is outside its children", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(userResp("admin"))));
    render(wrap(<Sidebar />));
    // Wait for the group to render.
    await waitFor(() =>
      expect(screen.getByTestId("nav-group-org-settings")).toHaveAttribute("data-collapsed"),
    );
    expect(screen.queryByTestId("nav-auth")).toBeNull();
  });

  it("manual toggle expands the group; navigating away collapses it again", async () => {
    server.use(http.get("/api/auth/me", () => HttpResponse.json(userResp("admin"))));
    const { rerender } = render(wrap(<Sidebar />));
    await waitFor(() =>
      expect(screen.getByTestId("nav-group-org-settings")).toHaveAttribute("data-collapsed"),
    );

    // Manual expand.
    screen.getByTestId("nav-group-org-settings").click();
    rerender(wrap(<Sidebar />));
    await waitFor(() =>
      expect(screen.getByTestId("nav-group-org-settings")).not.toHaveAttribute("data-collapsed"),
    );
    expect(screen.getByTestId("nav-auth")).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("yaaos.sidebar.collapse") ?? "{}")["org-settings"]).toBe(
      false,
    );

    // Simulate route change to /tickets → effect re-collapses.
    pathnameMock.mockReturnValue("/orgs/acme/tickets");
    rerender(wrap(<Sidebar />));
    await waitFor(() =>
      expect(screen.getByTestId("nav-group-org-settings")).toHaveAttribute("data-collapsed"),
    );
  });
});
