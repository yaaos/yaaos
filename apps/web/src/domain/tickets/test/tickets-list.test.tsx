import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

/**
 * Smoke tests for the Tickets list page. Mocks the underlying API hooks
 * so we don't depend on a backend; asserts that:
 *
 *   1. The filter chips render with status vocab labels.
 *   2. The empty-state branch renders when the API returns zero rows.
 *
 * Full filtering / Load-more behavior is exercised by the e2e PR-review
 * spec; this test is the cheap unit-level smoke.
 */

vi.mock("@core/api", () => ({
  getCurrentOrgSlug: () => "acme",
  useTickets: () => ({ data: [], isLoading: false, isError: false, refetch: vi.fn() }),
  useGithubRepositories: () => ({ data: { repositories: [] } }),
}));

vi.mock("@domain/auth", () => ({
  useCurrentUser: () => ({
    data: {
      user: { id: "u1", display_name: "Jane", primary_email: "j@x.test", emails: [] },
      memberships: [
        {
          org_id: "o1",
          slug: "acme",
          display_name: "Acme",
          role: "admin",
          handle: "jane",
        },
      ],
    },
  }),
}));

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, ...props }: { children: React.ReactNode; [k: string]: unknown }) => (
    <a {...(props as Record<string, string>)}>{children}</a>
  ),
}));

import { TicketsListPage } from "../TicketsListPage";

describe("TicketsListPage", () => {
  it("renders the status chips", () => {
    render(<TicketsListPage />);
    for (const s of ["running", "hitl", "done", "failed", "cancelled"]) {
      expect(screen.getByTestId(`tickets-filter-${s}`)).toBeInTheDocument();
    }
  });

  it("renders the empty state when there are zero tickets", () => {
    render(<TicketsListPage />);
    expect(screen.getByText(/No tickets yet/i)).toBeInTheDocument();
  });
});
