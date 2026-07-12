/**
 * Component/MSW tests for CodexSettings — the page is the OpenAI API key card.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "../../../../../../test/msw/server";
import { CodexSettings } from "../CodexSettings";

// ── Stubs ──────────────────────────────────────────────────────────────────

vi.mock("@tanstack/react-router", () => ({
  useRouterState: () => "/org/acme/settings/coding-agents/codex",
}));

const INSTALL = {
  plugin_id: "codex",
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
    http.get("/api/auth/me", () =>
      HttpResponse.json({
        user: { id: "u1", display_name: "Owner", primary_email: "o@x.com", emails: [] },
        memberships: [
          { org_id: "org-1", slug: "acme", role: "owner", handle: "owner", display_name: "Acme" },
        ],
      }),
    ),
    http.get("/api/coding-agents", () => HttpResponse.json([INSTALL])),
    http.get("/api/api-keys", () => HttpResponse.json([])),
  );
}

describe("CodexSettings (MSW)", () => {
  beforeEach(() => setupCommon());

  it("renders the OpenAI key card with not-set badge when no key is configured", async () => {
    render(wrap(<CodexSettings pluginId="codex" />));
    await waitFor(() => expect(screen.getByTestId("codex-key-not-set")).toBeInTheDocument());
    expect(screen.getByTestId("codex-key-input")).toBeInTheDocument();
    expect(screen.getByTestId("codex-key-save")).toBeInTheDocument();
  });

  it("shows the configured badge and summary when a key is already set", async () => {
    server.use(
      http.get("/api/api-keys", () =>
        HttpResponse.json([
          {
            provider: "openai",
            status: "configured",
            last_validated_at: null,
            last_used_at: null,
            updated_at: "2026-01-01T00:00:00Z",
          },
        ]),
      ),
    );
    render(wrap(<CodexSettings pluginId="codex" />));
    await waitFor(() => expect(screen.getByTestId("codex-key-configured")).toBeInTheDocument());
    expect(screen.getByTestId("codex-key-summary")).toBeInTheDocument();
    expect(screen.getByTestId("codex-key-test")).toBeInTheDocument();
  });

  it("shows not-installed message when plugin is absent from installs", async () => {
    server.use(http.get("/api/coding-agents", () => HttpResponse.json([])));
    render(wrap(<CodexSettings pluginId="codex" />));
    await waitFor(() => expect(screen.getByTestId("codex-not-installed")).toBeInTheDocument());
  });
});
