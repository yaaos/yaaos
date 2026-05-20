import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Mocks must run before the module under test is imported.
vi.mock("@core/api", () => ({
  getCurrentOrgSlug: () => "acme",
}));

vi.mock("@tanstack/react-router", () => ({
  useRouterState: () => ({ location: { pathname: "/orgs/acme/dashboard" } }),
}));

const currentUserMock = vi.fn();

vi.mock("@domain/auth", () => ({
  useCurrentUser: () => currentUserMock(),
  useLogoutAll: () => ({ mutate: vi.fn() }),
}));

import { Sidebar } from "../sidebar";

function userResp(role: "owner" | "admin" | "member") {
  return {
    data: {
      user: {
        id: "u1",
        display_name: "Jane Doe",
        primary_email: "j@x.test",
        emails: [],
      },
      orgs: [{ slug: "acme", display_name: "Acme", role, handle: "jane" }],
      current_org_slug: "acme",
    },
  };
}

describe("Sidebar", () => {
  beforeEach(() => {
    localStorage.clear();
    currentUserMock.mockReset();
  });

  it("renders top-level org-scoped links + the user card when expanded (snapshot-ish)", () => {
    currentUserMock.mockReturnValue(userResp("admin"));
    render(<Sidebar />);
    expect(screen.getByTestId("nav-dashboard")).toHaveAttribute("href", "/orgs/acme/dashboard");
    expect(screen.getByTestId("nav-dashboard")).toHaveAttribute("data-active");
    expect(screen.getByTestId("nav-tickets")).toHaveAttribute("href", "/orgs/acme/tickets");
    expect(screen.getByTestId("nav-memory")).toHaveAttribute("href", "/orgs/acme/memory");
    // Group header rendered; children visible by default (collapsed = false).
    expect(screen.getByTestId("nav-group-org-settings")).toBeInTheDocument();
    expect(screen.getByTestId("nav-auth")).toBeInTheDocument();
    expect(screen.getByTestId("nav-members")).toBeInTheDocument();
    expect(screen.getByTestId("nav-vcs")).toBeInTheDocument();
    expect(screen.getByTestId("nav-coding-agents")).toBeInTheDocument();
    expect(screen.getByTestId("nav-byok")).toBeInTheDocument();
    expect(screen.getByTestId("nav-audit")).toBeInTheDocument();
  });

  it("members see the Org Settings group but only the Members sub-item", () => {
    currentUserMock.mockReturnValue(userResp("member"));
    render(<Sidebar />);
    expect(screen.getByTestId("nav-dashboard")).toBeInTheDocument();
    expect(screen.getByTestId("nav-tickets")).toBeInTheDocument();
    // Group is visible (Members is in it) but only the Members link survives.
    expect(screen.getByTestId("nav-group-org-settings")).toBeInTheDocument();
    expect(screen.getByTestId("nav-members")).toBeInTheDocument();
    expect(screen.queryByTestId("nav-auth")).toBeNull();
    expect(screen.queryByTestId("nav-vcs")).toBeNull();
    expect(screen.queryByTestId("nav-coding-agents")).toBeNull();
    expect(screen.queryByTestId("nav-byok")).toBeNull();
    expect(screen.queryByTestId("nav-audit")).toBeNull();
  });

  it("shows admin-gated group items for owners", () => {
    currentUserMock.mockReturnValue(userResp("owner"));
    render(<Sidebar />);
    expect(screen.getByTestId("nav-group-org-settings")).toBeInTheDocument();
    expect(screen.getByTestId("nav-vcs")).toBeInTheDocument();
  });

  it("persists collapse state to localStorage per group", async () => {
    currentUserMock.mockReturnValue(userResp("admin"));
    const { rerender } = render(<Sidebar />);
    const groupBtn = screen.getByTestId("nav-group-org-settings");
    // Expanded by default.
    expect(groupBtn).not.toHaveAttribute("data-collapsed");

    // Click → collapsed.
    groupBtn.click();
    rerender(<Sidebar />);
    expect(screen.getByTestId("nav-group-org-settings")).toHaveAttribute("data-collapsed");
    const stored = localStorage.getItem("yaaos.sidebar.collapse");
    expect(stored).toBeTruthy();
    expect(JSON.parse(stored ?? "{}")["org-settings"]).toBe(true);
  });
});
