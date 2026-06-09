/**
 * Component/MSW tests for ClaudeCodeSettings — focused on the per-repo
 * skill-name card.
 *
 * Key invariant under test: the PUT request must use the raw `owner/repo`
 * as the trailing path segment (encodeURIComponent-encoded), NOT a
 * single-segment `{repo_external_id}` that would hit the 405 bug on
 * `%2F`-before-routing decode.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "../../../../../../test/msw/server";
import { ClaudeCodeSettings } from "../ClaudeCodeSettings";

// ── Stubs ──────────────────────────────────────────────────────────────────

vi.mock("@tanstack/react-router", () => ({
  useRouterState: () => "/orgs/acme/settings/coding-agents/claude_code",
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

const REPOS_RESPONSE = {
  repos: [
    { repo_external_id: "acme/web", skill_name: "code-review" },
    { repo_external_id: "acme/api", skill_name: null },
  ],
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
    http.get("/api/claude_code/repos", () => HttpResponse.json(REPOS_RESPONSE)),
  );
}

describe("ClaudeCodeSettings — repo skill names card (MSW)", () => {
  beforeEach(() => setupCommon());

  it("renders one text input per connected repo", async () => {
    render(wrap(<ClaudeCodeSettings pluginId="claude_code" />));
    // Wait for the repo rows to appear.
    await waitFor(() => expect(screen.getByTestId("repo-skill-row-acme/web")).toBeInTheDocument());
    expect(screen.getByTestId("repo-skill-row-acme/api")).toBeInTheDocument();

    // acme/web has a pre-filled value; acme/api is empty.
    const webInput = screen.getByTestId("repo-skill-input-acme/web") as HTMLInputElement;
    const apiInput = screen.getByTestId("repo-skill-input-acme/api") as HTMLInputElement;
    expect(webInput.value).toBe("code-review");
    expect(apiInput.value).toBe("");
  });

  it("Save fires PUT to the encoded owner/repo path (guards against 405 regression)", async () => {
    let capturedUrl: string | null = null;
    let capturedBody: unknown = null;

    // The PUT handler must match the encoded path. Using a wildcard here so
    // the test can assert the exact request URL, proving encodeURIComponent
    // was applied and the request actually reached a handler (not a 405).
    server.use(
      http.put("/api/claude_code/repos/:encodedId", async ({ request }) => {
        capturedUrl = request.url;
        capturedBody = await request.json();
        return HttpResponse.json({ repo_external_id: "acme/api", skill_name: "my-skill" });
      }),
    );

    render(wrap(<ClaudeCodeSettings pluginId="claude_code" />));
    await waitFor(() => expect(screen.getByTestId("repo-skill-row-acme/api")).toBeInTheDocument());

    const apiInput = screen.getByTestId("repo-skill-input-acme/api");
    fireEvent.change(apiInput, { target: { value: "my-skill" } });
    fireEvent.click(screen.getByTestId("repo-skill-save-acme/api"));

    await waitFor(() => expect(capturedBody).toMatchObject({ skill_name: "my-skill" }));
    // The request URL must contain the percent-encoded slash ("%2F"), not a bare
    // slash. A bare "/api/claude_code/repos/acme/api" would route to a different
    // path (or cause a 405) — this is the regression guard for the %2F bug.
    expect(capturedUrl).toContain("%2F");
    expect(capturedUrl).toContain("acme%2Fapi");
  });

  it("shows empty state when no repos are connected", async () => {
    server.use(http.get("/api/claude_code/repos", () => HttpResponse.json({ repos: [] })));
    render(wrap(<ClaudeCodeSettings pluginId="claude_code" />));
    await waitFor(() => expect(screen.getByTestId("repo-skills-empty")).toBeInTheDocument());
  });
});
