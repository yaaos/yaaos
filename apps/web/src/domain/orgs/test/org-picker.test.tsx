import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { server } from "../../../test/msw/server";
import { OrgPickerPage } from "../OrgPickerPage";

/**
 * Smoke tests for the Org Picker. Uses MSW to intercept:
 *   - GET /api/orgs/mine — controls the org list.
 *   - POST /api/orgs — create org mutation.
 */

// OrgPickerPage uses Link from @tanstack/react-router — stub it so the test
// doesn't need a full router context.
import { vi } from "vitest";
vi.mock("@tanstack/react-router", () => ({
  Link: ({
    to,
    params,
    children,
    ...props
  }: {
    to: string;
    params?: Record<string, string>;
    children: React.ReactNode;
  } & Record<string, unknown>) => {
    const href = params
      ? Object.entries(params).reduce((acc, [k, v]) => acc.replace(`$${k}`, v), to)
      : to;
    return (
      <a href={href} {...props}>
        {children}
      </a>
    );
  },
}));

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

describe("OrgPickerPage (MSW)", () => {
  beforeEach(() => {
    server.use(http.get("/api/orgs/mine", () => HttpResponse.json([])));
  });

  it("renders the EmptyState when there are no orgs", async () => {
    render(wrap(<OrgPickerPage />));
    await waitFor(() =>
      expect(screen.getByText(/don't belong to any organizations yet/i)).toBeInTheDocument(),
    );
  });

  it("renders one row per org with the role badge", async () => {
    server.use(
      http.get("/api/orgs/mine", () =>
        HttpResponse.json([
          { id: "o1", slug: "alpha", name: "Alpha", role: "admin", last_used_at: null },
          { id: "o2", slug: "beta", name: "Beta", role: "builder", last_used_at: null },
        ]),
      ),
    );
    render(wrap(<OrgPickerPage />));
    await waitFor(() =>
      expect(screen.getByTestId("org-picker-row-alpha")).toHaveTextContent("Alpha"),
    );
    expect(screen.getByTestId("org-picker-row-alpha")).toHaveTextContent("Admin");
    expect(screen.getByTestId("org-picker-row-beta")).toHaveTextContent("Beta");
    expect(screen.getByTestId("org-picker-row-beta")).toHaveTextContent("Builder");
  });

  it("Create button opens the modal + submit fires POST /api/orgs", async () => {
    let createBody: unknown = null;
    server.use(
      http.post("/api/orgs", async ({ request }) => {
        createBody = await request.json();
        return HttpResponse.json({ id: "o-new", slug: "new-org", name: "New Org", role: "admin" });
      }),
    );
    render(wrap(<OrgPickerPage />));
    await waitFor(() => expect(screen.getByTestId("org-picker-create")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("org-picker-create"));
    fireEvent.change(screen.getByTestId("create-org-name"), { target: { value: "New Org" } });
    fireEvent.change(screen.getByTestId("create-org-slug"), { target: { value: "new-org" } });
    fireEvent.click(screen.getByTestId("create-org-submit"));

    await waitFor(() => {
      expect(createBody).toEqual({ name: "New Org", slug: "new-org" });
    });
  });
});
