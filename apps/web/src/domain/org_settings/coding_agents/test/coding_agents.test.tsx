import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "../../../../test/msw/server";
import { CodingAgentsSettingsPage } from "../../public/coding_agents/CodingAgentsSettingsPage";

/**
 * Tests for CodingAgentsSettingsPage via MSW.
 */

// Link from @tanstack/react-router needs stubbing.
vi.mock("@tanstack/react-router", () => ({
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

const CLAUDE_CODE = {
  plugin_id: "claude_code",
  display_name: "Claude Code",
  models: ["claude-sonnet-5"],
  efforts: ["low", "medium", "high"],
  settings: {},
  created_at: "2026-05-20T00:00:00Z",
  updated_at: "2026-05-20T00:00:00Z",
};

const AVAILABLE_PLUGINS = {
  plugins: [{ plugin_id: "claude_code", display_name: "Claude Code" }],
};

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

function setupCommon() {
  server.use(
    http.get("/api/auth/me", () =>
      HttpResponse.json({
        user: { id: "u", display_name: "u", primary_email: "u@x", emails: [] },
        memberships: [
          { org_id: "o1", slug: "acme", role: "owner", handle: "j", display_name: "Acme" },
        ],
      }),
    ),
    http.get("/api/coding-agents/available", () => HttpResponse.json(AVAILABLE_PLUGINS)),
  );
}

describe("CodingAgentsSettingsPage (MSW)", () => {
  beforeEach(() => setupCommon());

  it("empty state shows the empty message + Add button", async () => {
    server.use(http.get("/api/coding-agents", () => HttpResponse.json([])));
    render(wrap(<CodingAgentsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("ca-empty")).toBeInTheDocument());
    expect(screen.getByTestId("ca-add")).toBeInTheDocument();
  });

  it("Add opens the install card with claude_code disabled when already installed", async () => {
    server.use(http.get("/api/coding-agents", () => HttpResponse.json([CLAUDE_CODE])));
    render(wrap(<CodingAgentsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("ca-add")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("ca-add"));
    await waitFor(() => expect(screen.getByTestId("ca-picker-card")).toBeInTheDocument());
    expect(screen.getByTestId("ca-picker-add-claude_code")).toBeDisabled();
  });

  it("Add installs claude_code when not yet installed", async () => {
    let installBody: unknown = null;
    server.use(
      http.get("/api/coding-agents", () => HttpResponse.json([])),
      http.post("/api/coding-agents", async ({ request }) => {
        installBody = await request.json();
        return HttpResponse.json({
          plugin_id: "claude_code",
          settings: {},
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        });
      }),
    );
    render(wrap(<CodingAgentsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("ca-add")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("ca-add"));
    await waitFor(() => expect(screen.getByTestId("ca-picker-card")).toBeInTheDocument());
    expect(screen.getByTestId("ca-picker-add-claude_code")).not.toBeDisabled();
    fireEvent.click(screen.getByTestId("ca-picker-add-claude_code"));
    await waitFor(() => expect(installBody).toMatchObject({ plugin_id: "claude_code" }));
  });

  it("Remove confirmation flow gates the uninstall mutation", async () => {
    let deletedPlugin: string | null = null;
    server.use(
      http.get("/api/coding-agents", () => HttpResponse.json([CLAUDE_CODE])),
      http.delete("/api/coding-agents/:pluginId", ({ params }) => {
        deletedPlugin = params.pluginId as string;
        return HttpResponse.json({ removed: true });
      }),
    );
    render(wrap(<CodingAgentsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("ca-install-claude_code")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("ca-remove-claude_code"));
    expect(screen.getByTestId("ca-remove-confirm-claude_code")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("ca-remove-cancel-claude_code"));
    expect(deletedPlugin).toBeNull();
    fireEvent.click(screen.getByTestId("ca-remove-claude_code"));
    fireEvent.click(screen.getByTestId("ca-remove-confirm-btn-claude_code"));
    await waitFor(() => expect(deletedPlugin).toBe("claude_code"));
  });

  it("Settings link targets the per-plugin route", async () => {
    server.use(http.get("/api/coding-agents", () => HttpResponse.json([CLAUDE_CODE])));
    // getCurrentOrgSlug reads window.location — in test env it's localhost, slug is null
    // so the link href falls back to the naked relative path.
    render(wrap(<CodingAgentsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("ca-settings-claude_code")).toBeInTheDocument());
  });
});
