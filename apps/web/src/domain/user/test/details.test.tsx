import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { server } from "../../../test/msw/server";
import { DetailsPage } from "../public/DetailsPage";

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

const BASE_USER = {
  user_id: "u1",
  display_name: "Jane Doe",
  github_username: null as string | null,
  emails: [
    { id: "e1", email: "jane@x.test", is_primary: true, verified: true },
    { id: "e2", email: "alt@x.test", is_primary: false, verified: true },
  ],
  memberships: [
    {
      org_id: "00000000-0000-0000-0000-000000000001",
      slug: "acme",
      display_name: "Acme",
      role: "owner",
      handle: "jane",
    },
    {
      org_id: "00000000-0000-0000-0000-000000000002",
      slug: "beta",
      display_name: "Beta",
      role: "builder",
      handle: "jdoe",
    },
  ],
};

// Default empty connections — override per-test when testing the connections section.
const EMPTY_CONNECTIONS = { connections: [] };

describe("DetailsPage (MSW)", () => {
  beforeEach(() => {
    server.use(
      http.get("/api/user/me", () => HttpResponse.json(BASE_USER)),
      http.get("/api/user/oauth/connections", () => HttpResponse.json(EMPTY_CONNECTIONS)),
    );
  });

  it("renders display name, per-org handles, emails, GitHub connect CTA", async () => {
    render(wrap(<DetailsPage />));
    await waitFor(() => expect(screen.getByTestId("display-name-input")).toHaveValue("Jane Doe"));
    expect(screen.getByTestId("handle-input-acme")).toHaveValue("jane");
    expect(screen.getByTestId("handle-input-beta")).toHaveValue("jdoe");
    expect(screen.getByTestId("handles-table")).toBeInTheDocument();
    expect(screen.getByTestId("emails-list")).toBeInTheDocument();
    expect(screen.getByText("jane@x.test")).toBeInTheDocument();
    expect(screen.getByText("alt@x.test")).toBeInTheDocument();
    expect(screen.queryByTestId("github-username")).toBeNull();
  });

  it("renders verified GitHub state when username is set", async () => {
    server.use(
      http.get("/api/user/me", () =>
        HttpResponse.json({ ...BASE_USER, github_username: "octocat" }),
      ),
    );
    render(wrap(<DetailsPage />));
    await waitFor(() =>
      expect(screen.getByTestId("github-username")).toHaveTextContent("@octocat"),
    );
    expect(screen.getByTestId("github-clear")).toBeInTheDocument();
  });

  it("renders connections section with a not-connected card", async () => {
    server.use(
      http.get("/api/user/oauth/connections", () =>
        HttpResponse.json({
          connections: [
            {
              provider_id: "codex",
              display_name: "Codex (ChatGPT)",
              connect_hint: "Authorize yaaos in ChatGPT settings.",
              status: "not_connected",
              external_account_id: null,
              connected_at: null,
              needs_reauth_reason: null,
            },
          ],
        }),
      ),
    );
    render(wrap(<DetailsPage />));
    await waitFor(() => expect(screen.getByTestId("connections-section")).toBeInTheDocument());
    expect(screen.getByTestId("connection-row-codex")).toBeInTheDocument();
    expect(screen.getByTestId("connection-connect-codex")).toBeInTheDocument();
  });

  it("renders connected card with Disconnect button", async () => {
    server.use(
      http.get("/api/user/oauth/connections", () =>
        HttpResponse.json({
          connections: [
            {
              provider_id: "codex",
              display_name: "Codex (ChatGPT)",
              connect_hint: "Authorize yaaos in ChatGPT settings.",
              status: "connected",
              external_account_id: "chatgpt-acct-123",
              connected_at: new Date().toISOString(),
              needs_reauth_reason: null,
            },
          ],
        }),
      ),
    );
    render(wrap(<DetailsPage />));
    await waitFor(() =>
      expect(screen.getByTestId("connection-disconnect-codex")).toBeInTheDocument(),
    );
    expect(screen.getByText(/chatgpt-acct-123/)).toBeInTheDocument();
  });

  it("renders needs_reauth card with reason text and reconnect button", async () => {
    server.use(
      http.get("/api/user/oauth/connections", () =>
        HttpResponse.json({
          connections: [
            {
              provider_id: "codex",
              display_name: "Codex (ChatGPT)",
              connect_hint: "Authorize yaaos in ChatGPT settings.",
              status: "needs_reauth",
              external_account_id: null,
              connected_at: null,
              needs_reauth_reason: "token refresh rejected (invalid_grant)",
            },
          ],
        }),
      ),
    );
    render(wrap(<DetailsPage />));
    await waitFor(() => expect(screen.getByTestId("connections-section")).toBeInTheDocument());
    expect(screen.getByTestId("connection-row-codex")).toBeInTheDocument();
    // Reconnect button shown (not connected)
    expect(screen.getByTestId("connection-connect-codex")).toBeInTheDocument();
    // Reason text surfaced to the user
    expect(screen.getByText(/invalid_grant/)).toBeInTheDocument();
  });
});
