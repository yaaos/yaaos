/**
 * Component/MSW tests for ClaudeCodeSettings — the page is the Anthropic API
 * key card plus the danger zone; skill selection lives on pipeline stage
 * definitions, never here.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "../../../../../../test/msw/server";
import { ClaudeCodeSettings } from "../ClaudeCodeSettings";

// ── Stubs ──────────────────────────────────────────────────────────────────

vi.mock("@tanstack/react-router", () => ({
  useRouterState: () => "/org/acme/settings/coding-agents/claude_code",
}));

const ME_RESPONSE = {
  user: { id: "u1", display_name: "Owner", primary_email: "o@x.com", emails: [] },
  memberships: [
    { org_id: "org-1", slug: "acme", role: "owner", handle: "owner", display_name: "Acme" },
  ],
};

const INSTALL = {
  plugin_id: "claude_code",
  settings: {},
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

function setupCommon() {
  server.use(
    http.get("/api/auth/me", () => HttpResponse.json(ME_RESPONSE)),
    http.get("/api/coding-agents", () => HttpResponse.json([INSTALL])),
    http.get("/api/api-keys", () => HttpResponse.json([])),
  );
}

describe("ClaudeCodeSettings (MSW)", () => {
  beforeEach(() => setupCommon());

  it("renders the API key card and the danger zone — nothing else", async () => {
    render(wrap(<ClaudeCodeSettings pluginId="claude_code" />));
    await waitFor(() => expect(screen.getByTestId("cc-key-not-set")).toBeInTheDocument());
    expect(screen.getByTestId("cc-uninstall-button")).toBeInTheDocument();
    // The reviewer-era repo-skill card must not resurface.
    expect(screen.queryByText("Repo skill names")).not.toBeInTheDocument();
  });

  it("shows the configured badge when a key is already set", async () => {
    server.use(
      http.get("/api/api-keys", () =>
        HttpResponse.json([
          {
            provider: "anthropic",
            status: "configured",
            last_validated_at: null,
            last_used_at: null,
            updated_at: "2026-01-01T00:00:00Z",
          },
        ]),
      ),
    );
    render(wrap(<ClaudeCodeSettings pluginId="claude_code" />));
    await waitFor(() => expect(screen.getByTestId("cc-key-configured")).toBeInTheDocument());
    expect(screen.getByTestId("cc-key-summary")).toBeInTheDocument();
  });
});
