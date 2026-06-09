import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { describe, expect, it, vi } from "vitest";
import { server } from "../../../test/msw/server";
import { TicketDetailPage } from "../public/TicketDetailPage";

/**
 * Smoke tests for TicketDetailPage. Uses MSW to intercept all ticket/reviewer
 * endpoints; asserts that the header, stage indicator, and tab strip render
 * against a representative ticket.
 *
 * StageIndicator is sourced from the dedicated workflow-runs endpoint rather
 * than `ticket.stages` (removed). The Re-run button and cost-estimate modal
 * are gone — only Cancel remains for non-terminal tickets.
 */

vi.mock("@tanstack/react-router", () => ({
  useParams: () => ({ ticketId: "t1" }),
}));

// SSE workflow stream — not relevant for these tests; live step pane only
// renders when a step is in state "running" which none of our fixtures use.
vi.mock("@core/sse/public/workflow_activity", () => ({
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
  current_stage: "pr_review_v1",
  findings_count: 0,
  max_severity: null,
  builder_kind: "user",
  builder_display_name: "alice",
  builder: { kind: "user", display_name: "alice" },
};

const WORKFLOW_RUNS = [
  {
    id: "wfx-1",
    workflow_name: "pr_review_v1",
    workflow_version: 1,
    state: "running",
    current_step_id: "step-check",
    failure_reason: null,
    created_at: "2026-05-23T00:00:00Z",
    updated_at: "2026-05-23T00:00:00Z",
    steps: [
      {
        step_id: "step-check",
        command_kind: "CheckShouldReview",
        state: "done",
        started_at: "2026-05-23T00:00:00Z",
        completed_at: "2026-05-23T00:01:00Z",
      },
    ],
  },
];

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

function withHandlers() {
  server.use(
    http.get("/api/tickets/t1", () => HttpResponse.json(TICKET)),
    http.get("/api/tickets/t1/workflow-runs", () => HttpResponse.json(WORKFLOW_RUNS)),
    http.get("/api/reviewer/findings/by-ticket/t1", () => HttpResponse.json([])),
    http.get("/api/tickets/t1/hitl/history", () => HttpResponse.json([])),
  );
}

describe("TicketDetailPage (MSW)", () => {
  it("renders the title + status pill", async () => {
    withHandlers();
    render(wrap(<TicketDetailPage />));
    await waitFor(() => expect(screen.getByTestId("ticket-detail")).toBeInTheDocument());
    expect(screen.getByText("Add /metrics endpoint")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-status-running")).toBeInTheDocument();
  });

  it("renders stage indicator sourced from workflow-runs endpoint", async () => {
    withHandlers();
    render(wrap(<TicketDetailPage />));
    await waitFor(() => expect(screen.getByTestId("stage-indicator")).toBeInTheDocument());
    expect(screen.getByTestId("stage-pr_review_v1")).toBeInTheDocument();
  });

  it("renders all 3 tabs in the tab strip", async () => {
    withHandlers();
    render(wrap(<TicketDetailPage />));
    await waitFor(() => expect(screen.getByTestId("ticket-tab-findings")).toBeInTheDocument());
    expect(screen.getByTestId("ticket-tab-activity")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-tab-hitl")).toBeInTheDocument();
  });

  it("exposes Cancel button when ticket is non-terminal, no Re-run button", async () => {
    withHandlers();
    render(wrap(<TicketDetailPage />));
    await waitFor(() => expect(screen.getByTestId("ticket-cancel-button")).toBeInTheDocument());
    expect(screen.queryByTestId("ticket-rerun-button")).toBeNull();
  });

  it("no Cancel button when ticket is done", async () => {
    server.use(
      http.get("/api/tickets/t1", () => HttpResponse.json({ ...TICKET, status: "done" })),
      http.get("/api/tickets/t1/workflow-runs", () => HttpResponse.json(WORKFLOW_RUNS)),
      http.get("/api/reviewer/findings/by-ticket/t1", () => HttpResponse.json([])),
      http.get("/api/tickets/t1/hitl/history", () => HttpResponse.json([])),
    );
    render(wrap(<TicketDetailPage />));
    await waitFor(() => expect(screen.getByTestId("ticket-detail")).toBeInTheDocument());
    expect(screen.queryByTestId("ticket-cancel-button")).toBeNull();
  });

  it("shows step label rows in activity tab", async () => {
    withHandlers();
    render(wrap(<TicketDetailPage />));
    await waitFor(() => expect(screen.getByTestId("ticket-detail")).toBeInTheDocument());
    const activityTab = screen.getByTestId("ticket-tab-activity");
    activityTab.click();
    await waitFor(() => expect(screen.getByTestId("step-tree")).toBeInTheDocument());
    expect(screen.getByTestId("step-row-step-check")).toBeInTheDocument();
  });
});
