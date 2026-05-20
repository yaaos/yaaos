import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const currentUserMock = vi.fn();

vi.mock("@core/api", () => ({
  getCurrentOrgSlug: () => "acme",
}));

vi.mock("@domain/auth", () => ({
  useCurrentUser: () => currentUserMock(),
}));

import { OrgSettingsLayout } from "../OrgSettingsLayout";

function userResp(role: "owner" | "admin" | "member") {
  return {
    data: {
      orgs: [{ slug: "acme", role, handle: "x", display_name: "Acme" }],
      current_org_slug: "acme",
      user: { id: "u", display_name: "u", primary_email: "u@x", emails: [] },
    },
  };
}

describe("OrgSettingsLayout", () => {
  beforeEach(() => currentUserMock.mockReset());

  it("admin sees all six tabs and active tab is highlighted", () => {
    currentUserMock.mockReturnValue(userResp("admin"));
    render(<OrgSettingsLayout active="auth">child</OrgSettingsLayout>);
    expect(screen.getByTestId("tab-auth")).toHaveAttribute("data-active");
    for (const id of ["auth", "members", "vcs", "coding-agents", "byok", "audit"]) {
      expect(screen.getByTestId(`tab-${id}`)).toBeInTheDocument();
    }
  });

  it("member sees only the Members tab", () => {
    currentUserMock.mockReturnValue(userResp("member"));
    render(<OrgSettingsLayout active="members">child</OrgSettingsLayout>);
    expect(screen.getByTestId("tab-members")).toBeInTheDocument();
    for (const id of ["auth", "vcs", "coding-agents", "byok", "audit"]) {
      expect(screen.queryByTestId(`tab-${id}`)).toBeNull();
    }
  });

  it("renders children below the tab strip", () => {
    currentUserMock.mockReturnValue(userResp("owner"));
    render(
      <OrgSettingsLayout active="auth">
        <div data-testid="content">section content</div>
      </OrgSettingsLayout>,
    );
    expect(screen.getByTestId("content")).toBeInTheDocument();
  });
});
