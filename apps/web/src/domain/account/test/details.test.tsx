import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type React from "react";
import { describe, expect, it, vi } from "vitest";

const accountMeMock = vi.fn();

vi.mock("../queries", () => ({
  useAccountMe: () => accountMeMock(),
  useUpdateDisplayName: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateOrgHandle: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useClearGithubUsername: () => ({ mutate: vi.fn(), isPending: false }),
}));

import { DetailsPage } from "../DetailsPage";

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

const baseData = {
  user_id: "u1",
  display_name: "Jane Doe",
  github_username: null as string | null,
  emails: [
    { id: "e1", email: "jane@x.test", is_primary: true, verified: true },
    { id: "e2", email: "alt@x.test", is_primary: false, verified: true },
  ],
  orgs: [
    {
      org_id: "00000000-0000-0000-0000-000000000001",
      slug: "acme",
      display_name: "Acme",
      role: "owner" as const,
      handle: "jane",
    },
    {
      org_id: "00000000-0000-0000-0000-000000000002",
      slug: "beta",
      display_name: "Beta",
      role: "builder" as const,
      handle: "jdoe",
    },
  ],
};

describe("DetailsPage", () => {
  it("loading state when query pending", () => {
    accountMeMock.mockReturnValue({ data: null, isLoading: true });
    render(wrap(<DetailsPage />));
    expect(screen.getByText(/Loading/)).toBeInTheDocument();
  });

  it("renders display name, per-org handles, emails, GitHub connect CTA", () => {
    accountMeMock.mockReturnValue({ data: baseData, isLoading: false });
    render(wrap(<DetailsPage />));
    expect(screen.getByTestId("display-name-input")).toHaveValue("Jane Doe");
    // Two handle rows, each editable + savable.
    expect(screen.getByTestId("handle-input-acme")).toHaveValue("jane");
    expect(screen.getByTestId("handle-input-beta")).toHaveValue("jdoe");
    expect(screen.getByTestId("handles-table")).toBeInTheDocument();
    // Emails listed (read-only).
    expect(screen.getByTestId("emails-list")).toBeInTheDocument();
    expect(screen.getByText("jane@x.test")).toBeInTheDocument();
    expect(screen.getByText("alt@x.test")).toBeInTheDocument();
    // GitHub not yet connected → Connect CTA.
    expect(screen.getByTestId("github-connect")).toBeInTheDocument();
    expect(screen.queryByTestId("github-username")).toBeNull();
  });

  it("renders verified GitHub state when username is set", () => {
    accountMeMock.mockReturnValue({
      data: { ...baseData, github_username: "octocat" },
      isLoading: false,
    });
    render(wrap(<DetailsPage />));
    expect(screen.getByTestId("github-username")).toHaveTextContent("@octocat");
    expect(screen.getByTestId("github-reverify")).toBeInTheDocument();
    expect(screen.getByTestId("github-clear")).toBeInTheDocument();
  });
});
