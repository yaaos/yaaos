import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Mocks must run before the module under test is imported.
vi.mock("@core/api", () => ({
  getCurrentOrgSlug: () => "acme",
  useCurrentOrgSlug: () => "acme",
  useMyOrgs: () => ({
    data: [{ id: "o1", slug: "acme", name: "Acme", role: "admin", last_used_at: null }],
  }),
  useConfigStatus: () => ({ data: { configured: true, missing: [], admins: [] } }),
  useNotificationsPopover: () => ({ data: { items: [], unread_count: 0 } }),
  useMarkNotificationRead: () => ({ mutate: vi.fn() }),
  useMarkAllNotificationsRead: () => ({ mutate: vi.fn(), isPending: false }),
}));

const pathnameMock = vi.fn(() => "/orgs/acme/dashboard");

vi.mock("@tanstack/react-router", () => ({
  useRouterState: () => ({ location: { pathname: pathnameMock() } }),
  // Stub `Link` so it renders a plain `<a>` — production code uses it for
  // SPA nav, the assertions in this file only care about the rendered href.
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

const currentUserMock = vi.fn();

vi.mock("@domain/auth", () => ({
  useCurrentUser: () => currentUserMock(),
  useLogout: () => ({ mutate: vi.fn() }),
}));

import { Sidebar } from "../sidebar";

function userResp(role: "owner" | "admin" | "builder") {
  return {
    data: {
      user: {
        id: "u1",
        display_name: "Jane Doe",
        primary_email: "j@x.test",
        emails: [],
      },
      memberships: [{ org_id: "o1", slug: "acme", display_name: "Acme", role, handle: "jane" }],
    },
  };
}

describe("Sidebar", () => {
  beforeEach(() => {
    localStorage.clear();
    currentUserMock.mockReset();
    pathnameMock.mockReturnValue("/orgs/acme/dashboard");
  });

  it("renders top-level org-scoped links + the user card when expanded (snapshot-ish)", () => {
    // Put the user inside a settings sub-route so the group is expanded
    // and the children render in the DOM for testid lookup.
    pathnameMock.mockReturnValue("/orgs/acme/settings/auth");
    currentUserMock.mockReturnValue(userResp("admin"));
    render(<Sidebar />);
    expect(screen.getByTestId("nav-dashboard")).toHaveAttribute("href", "/orgs/acme/dashboard");
    expect(screen.getByTestId("nav-tickets")).toHaveAttribute("href", "/orgs/acme/tickets");
    expect(screen.getByTestId("nav-lessons")).toHaveAttribute("href", "/orgs/acme/lessons");
    expect(screen.getByTestId("nav-group-org-settings")).toBeInTheDocument();
    expect(screen.getByTestId("nav-auth")).toBeInTheDocument();
    expect(screen.getByTestId("nav-auth")).toHaveAttribute("data-active");
    expect(screen.getByTestId("nav-members")).toBeInTheDocument();
    expect(screen.getByTestId("nav-vcs")).toBeInTheDocument();
    expect(screen.getByTestId("nav-coding-agents")).toBeInTheDocument();
    expect(screen.getByTestId("nav-api-keys")).toBeInTheDocument();
    expect(screen.getByTestId("nav-audit")).toBeInTheDocument();
  });

  it("members see the Org Settings group but only the Members sub-item", () => {
    pathnameMock.mockReturnValue("/orgs/acme/settings/members");
    currentUserMock.mockReturnValue(userResp("builder"));
    render(<Sidebar />);
    expect(screen.getByTestId("nav-dashboard")).toBeInTheDocument();
    expect(screen.getByTestId("nav-tickets")).toBeInTheDocument();
    expect(screen.getByTestId("nav-group-org-settings")).toBeInTheDocument();
    expect(screen.getByTestId("nav-members")).toBeInTheDocument();
    expect(screen.queryByTestId("nav-auth")).toBeNull();
    expect(screen.queryByTestId("nav-vcs")).toBeNull();
    expect(screen.queryByTestId("nav-coding-agents")).toBeNull();
    expect(screen.queryByTestId("nav-api-keys")).toBeNull();
    expect(screen.queryByTestId("nav-audit")).toBeNull();
  });

  it("shows admin-gated group items for owners", () => {
    pathnameMock.mockReturnValue("/orgs/acme/settings/vcs");
    currentUserMock.mockReturnValue(userResp("owner"));
    render(<Sidebar />);
    expect(screen.getByTestId("nav-group-org-settings")).toBeInTheDocument();
    expect(screen.getByTestId("nav-vcs")).toBeInTheDocument();
  });

  it("auto-collapses the group when the route is outside its children", () => {
    // /dashboard is not a settings sub-path → group must be collapsed and
    // children must not render in the DOM.
    currentUserMock.mockReturnValue(userResp("admin"));
    render(<Sidebar />);
    expect(screen.getByTestId("nav-group-org-settings")).toHaveAttribute("data-collapsed");
    expect(screen.queryByTestId("nav-auth")).toBeNull();
  });

  it("manual toggle expands the group; navigating away collapses it again", () => {
    currentUserMock.mockReturnValue(userResp("admin"));
    const { rerender } = render(<Sidebar />);
    // Auto-collapsed on /dashboard.
    expect(screen.getByTestId("nav-group-org-settings")).toHaveAttribute("data-collapsed");

    // Manual expand.
    screen.getByTestId("nav-group-org-settings").click();
    rerender(<Sidebar />);
    expect(screen.getByTestId("nav-group-org-settings")).not.toHaveAttribute("data-collapsed");
    expect(screen.getByTestId("nav-auth")).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("yaaos.sidebar.collapse") ?? "{}")["org-settings"]).toBe(
      false,
    );

    // Simulate route change to /tickets → effect re-collapses.
    pathnameMock.mockReturnValue("/orgs/acme/tickets");
    rerender(<Sidebar />);
    expect(screen.getByTestId("nav-group-org-settings")).toHaveAttribute("data-collapsed");
  });
});
