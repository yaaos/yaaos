import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { describe, expect, it, vi } from "vitest";
import { server } from "../../../test/msw/server";
import { TicketDetailPage } from "../TicketDetailPage";

/**
 * Smoke tests for TicketDetailPage. Uses MSW to intercept all ticket/reviewer
 * endpoints; asserts that the header, stage indicator, and tab strip render
 * against a representative ticket.
 */

vi.mock("@tanstack/react-router", () => ({
  useParams: () => ({ ticketId: "t1" }),
}));

// SSE workflow stream — not relevant for these tests.
vi.mock("@core/sse", () => ({
  useWorkflowActivityStream: () => [],
}));

const TICKET = {
  id: "t1",
  org_id: "o1",
  source: "github_pr",
  source_external_id: "x/y#1",
  title: "Add /metrics endpoint",
  description: null,
  status: "running",
  plugin_id: "github",
  repo_external_id: "x/y",
  pr_id: "p1",
  pr_number: 42,
  pr_html_url: "https://x/y/pull/42",
  author_login: "alice",
  is_draft: false,
  created_at: "2026-05-23T00:00:00Z",
  updated_at: "2026-05-23T00:00:00Z",
  current_stage: "Review",
  findings_count: 0,
  max_severity: null,
  builder_kind: "user",
  builder_display_name: "alice",
  stages: [
    {
      name: "Review",
      state: "running",
      attempt_count: 1,
      current_attempt: 1,
      started_at: "2026-05-23T00:00:00Z",
      completed_at: null,
      workflow_execution_id: "wfx-1",
    },
  ],
  builder: { kind: "user", display_name: "alice" },
};

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

function withHandlers() {
  server.use(
    http.get("/api/tickets/t1", () => HttpResponse.json(TICKET)),
    http.get("/api/reviewer/findings/by-ticket/t1", () => HttpResponse.json([])),
    http.get("/api/reviewer/jobs/by-ticket/t1", () => HttpResponse.json([])),
    http.get("/api/tickets/t1/hitl/history", () => HttpResponse.json([])),
    http.get("/api/tickets/t1/audit", () => HttpResponse.json([])),
    http.get("/api/reviewer/reviews/by-ticket/t1", () => HttpResponse.json([])),
    http.get("/api/reviewer/conversations/by-ticket/t1", () => HttpResponse.json([])),
  );
}

describe("TicketDetailPage (MSW)", () => {
  it("renders the title + repo + status pill", async () => {
    withHandlers();
    render(wrap(<TicketDetailPage />));
    await waitFor(() => expect(screen.getByTestId("ticket-detail")).toBeInTheDocument());
    expect(screen.getByText("Add /metrics endpoint")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-status-running")).toBeInTheDocument();
  });

  it("renders the stage indicator from ticket.stages", async () => {
    withHandlers();
    render(wrap(<TicketDetailPage />));
    await waitFor(() => expect(screen.getByTestId("stage-indicator")).toBeInTheDocument());
    expect(screen.getByTestId("stage-Review")).toBeInTheDocument();
  });

  it("renders all 3 tabs in the tab strip", async () => {
    withHandlers();
    render(wrap(<TicketDetailPage />));
    await waitFor(() => expect(screen.getByTestId("ticket-tab-findings")).toBeInTheDocument());
    expect(screen.getByTestId("ticket-tab-activity")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-tab-hitl")).toBeInTheDocument();
  });

  it("exposes both Cancel and Re-run buttons when ticket is non-terminal", async () => {
    withHandlers();
    render(wrap(<TicketDetailPage />));
    await waitFor(() => expect(screen.getByTestId("ticket-cancel-button")).toBeInTheDocument());
    expect(screen.getByTestId("ticket-rerun-button")).toBeInTheDocument();
  });
});
