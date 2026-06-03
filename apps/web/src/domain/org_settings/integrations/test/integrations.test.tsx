import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { describe, expect, it } from "vitest";
import { server } from "../../../../test/msw/server";
import { IntegrationsSettingsPage } from "../IntegrationsSettingsPage";

/**
 * Tests for IntegrationsSettingsPage via MSW.
 */

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

const LINEAR_NOT_SET = {
  provider: "linear",
  status: "not_set",
  enabled: null,
  upstream_identity: null,
  last_validated_at: null,
  last_refresh_failed_at: null,
  allowed_tools: [],
};

describe("IntegrationsSettingsPage (MSW)", () => {
  it("renders not_set provider with Connect button", async () => {
    server.use(http.get("/api/mcp-proxy", () => HttpResponse.json([LINEAR_NOT_SET])));
    render(wrap(<IntegrationsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("badge-linear-disconnected")).toBeTruthy());
    const link = screen.getByTestId("connect-linear") as HTMLAnchorElement;
    expect(link.href).toContain("/api/mcp-proxy/linear/connect");
  });

  it("renders connected provider with allowlist + Disconnect confirm flow", async () => {
    let deletedProvider: string | null = null;
    server.use(
      http.get("/api/mcp-proxy", () =>
        HttpResponse.json([
          {
            provider: "notion",
            status: "configured",
            enabled: true,
            upstream_identity: "notion-bot",
            last_validated_at: "2026-05-20T10:00:00Z",
            last_refresh_failed_at: null,
            allowed_tools: ["update_page"],
          },
        ]),
      ),
      http.delete("/api/mcp-proxy/:provider", ({ params }) => {
        deletedProvider = params.provider as string;
        return HttpResponse.json({ removed: true });
      }),
    );
    render(wrap(<IntegrationsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("badge-notion-connected")).toBeTruthy());
    expect(screen.getByTestId("allow-chip-notion-update_page")).toBeTruthy();
    fireEvent.click(screen.getByTestId("disconnect-notion"));
    expect(screen.getByTestId("disconnect-confirm-notion")).toBeTruthy();
    fireEvent.click(screen.getByTestId("disconnect-confirm-btn-notion"));
    await waitFor(() => expect(deletedProvider).toBe("notion"));
  });

  it("shows Reconnect-required badge for broken provider", async () => {
    server.use(
      http.get("/api/mcp-proxy", () =>
        HttpResponse.json([
          {
            provider: "linear",
            status: "broken",
            enabled: true,
            upstream_identity: "linear-bot",
            last_validated_at: null,
            last_refresh_failed_at: "2026-05-20T10:00:00Z",
            allowed_tools: [],
          },
        ]),
      ),
    );
    render(wrap(<IntegrationsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("badge-linear-broken")).toBeTruthy());
  });

  it("toggles enabled via PATCH", async () => {
    let patchBody: unknown = null;
    server.use(
      http.get("/api/mcp-proxy", () =>
        HttpResponse.json([
          {
            provider: "linear",
            status: "configured",
            enabled: true,
            upstream_identity: "linear-bot",
            last_validated_at: null,
            last_refresh_failed_at: null,
            allowed_tools: [],
          },
        ]),
      ),
      http.patch("/api/mcp-proxy/:provider", async ({ request }) => {
        patchBody = await request.json();
        return HttpResponse.json({
          provider: "linear",
          status: "configured",
          enabled: false,
          upstream_identity: "linear-bot",
          last_validated_at: null,
          last_refresh_failed_at: null,
          allowed_tools: [],
        });
      }),
    );
    render(wrap(<IntegrationsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("enabled-linear")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("enabled-linear"));
    await waitFor(() => expect(patchBody).toEqual({ enabled: false }));
  });

  it("adds and removes allowlist entries", async () => {
    const patchBodies: unknown[] = [];
    server.use(
      http.get("/api/mcp-proxy", () =>
        HttpResponse.json([
          {
            provider: "linear",
            status: "configured",
            enabled: true,
            upstream_identity: "linear-bot",
            last_validated_at: null,
            last_refresh_failed_at: null,
            allowed_tools: ["update_issue"],
          },
        ]),
      ),
      http.patch("/api/mcp-proxy/:provider", async ({ request }) => {
        const body = await request.json();
        patchBodies.push(body);
        return HttpResponse.json({
          provider: "linear",
          status: "configured",
          enabled: true,
          upstream_identity: "linear-bot",
          last_validated_at: null,
          last_refresh_failed_at: null,
          allowed_tools: (body as { allowed_tools: string[] }).allowed_tools ?? [],
        });
      }),
    );
    render(wrap(<IntegrationsSettingsPage />));
    await waitFor(() =>
      expect(screen.getByTestId("allow-remove-linear-update_issue")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("allow-remove-linear-update_issue"));
    await waitFor(() => expect(patchBodies[0]).toEqual({ allowed_tools: [] }));

    const input = screen.getByTestId("allow-input-linear") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "create_comment" } });
    fireEvent.click(screen.getByTestId("allow-add-linear"));
    await waitFor(() =>
      expect(patchBodies[1]).toEqual({ allowed_tools: ["update_issue", "create_comment"] }),
    );
  });
});
