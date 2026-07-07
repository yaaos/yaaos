import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type React from "react";
import { describe, expect, it, vi } from "vitest";
import { server } from "../../../test/msw/server";
import { TicketDetailPage } from "../public/TicketDetailPage";

/**
 * Component tests for the reworked TicketDetailPage: header + tab strip,
 * and the Overview tab's three `RunOverview.status` branches (paused /
 * in_flight / terminal) plus the disabled-actions "Waiting on {names}."
 * state when the server reports `can_respond: false`.
 */

vi.mock("@tanstack/react-router", () => ({
  useParams: () => ({ ticketId: "t1" }),
}));

const TICKET = {
  id: "t1",
  org_id: "o1",
  source: "github_pr",
  source_external_id: "x/y#1",
  title: "Add /metrics endpoint",
  description: null,
  status: "hitl",
  plugin_id: "github",
  repo_external_id: "x/y",
  pr_id: "p1",
  pr_number: 42,
  pr_html_url: "https://x/y/pull/42",
  author_login: "alice",
  is_draft: false,
  created_at: "2026-05-23T00:00:00Z",
  updated_at: "2026-05-23T00:00:00Z",
  current_stage: null,
  findings_count: 0,
  max_severity: null,
  builder_kind: "user" as const,
  builder_display_name: "alice",
  builder: { kind: "user" as const, display_name: "alice" },
};

const PAUSED_OVERVIEW = {
  status: "paused",
  pause: {
    pause_id: "pause-1",
    stage_name: "write-spec",
    tripped: { always_hitl: true },
    artifact_id: null,
    residuals: [],
    escalation_logins: ["alice"],
    can_respond: true,
  },
};

const RUN = {
  id: "run-1",
  pipeline_name: "dev",
  state: "paused",
  kickoff: { intake_point_id: "test", actor_kind: "user", actor_login: "alice", input_text: null },
  created_at: "2026-05-23T00:00:00Z",
  completed_at: null,
  failure_reason: null,
  stages: [
    {
      id: "se-1",
      stage_index: 0,
      kind: "skill",
      stage_name: "write-spec",
      status: "completed",
      confidence: "high",
      review_iterations: 0,
      boundary_outcome: "paused",
      artifact_ids: [],
      action_result: null,
      decisions: [],
      failure_reason: null,
      started_at: "2026-05-23T00:00:00Z",
      completed_at: "2026-05-23T00:01:00Z",
    },
  ],
};

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

function withBaseHandlers() {
  server.use(
    http.get("/api/tickets/t1", () => HttpResponse.json(TICKET)),
    http.get("/api/tickets/t1/audit", () => HttpResponse.json([])),
    http.get("/api/pipelines/runs", () => HttpResponse.json({ runs: [RUN] })),
    http.get("/api/artifacts", () => HttpResponse.json({ artifacts: [] })),
  );
}

describe("TicketDetailPage (MSW)", () => {
  it("renders the title, status pill, and tab strip", async () => {
    withBaseHandlers();
    server.use(http.get("/api/pipelines/runs/overview", () => HttpResponse.json(PAUSED_OVERVIEW)));
    render(wrap(<TicketDetailPage />));
    await waitFor(() => expect(screen.getByTestId("ticket-detail")).toBeInTheDocument());
    expect(screen.getByText("Add /metrics endpoint")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-status-hitl")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-tab-overview")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-tab-runs")).toBeInTheDocument();
    expect(screen.getByTestId("ticket-tab-artifacts")).toBeInTheDocument();
  });

  it("paused: renders the attention block with actions enabled when can_respond is true", async () => {
    withBaseHandlers();
    server.use(http.get("/api/pipelines/runs/overview", () => HttpResponse.json(PAUSED_OVERVIEW)));
    render(wrap(<TicketDetailPage />));
    const block = await screen.findByTestId("attention-block");
    expect(block).toHaveAttribute("data-state", "paused");
    expect(screen.getByTestId("approve-run")).not.toBeDisabled();
    expect(screen.getByTestId("kill-run")).not.toBeDisabled();
    expect(screen.queryByTestId("pause-waiting-on")).not.toBeInTheDocument();
  });

  it("paused: disables actions and shows 'Waiting on {names}.' when can_respond is false", async () => {
    withBaseHandlers();
    server.use(
      http.get("/api/pipelines/runs/overview", () =>
        HttpResponse.json({
          ...PAUSED_OVERVIEW,
          pause: { ...PAUSED_OVERVIEW.pause, can_respond: false, escalation_logins: ["bob"] },
        }),
      ),
    );
    render(wrap(<TicketDetailPage />));
    await screen.findByTestId("attention-block");
    expect(screen.getByText("Waiting on bob.")).toBeInTheDocument();
    expect(screen.getByTestId("approve-run")).toBeDisabled();
    expect(screen.getByTestId("kill-run")).toBeDisabled();
  });

  it("in_flight: renders the live card with a Cancel action", async () => {
    withBaseHandlers();
    server.use(
      http.get("/api/pipelines/runs/overview", () =>
        HttpResponse.json({ status: "in_flight", run: RUN }),
      ),
    );
    render(wrap(<TicketDetailPage />));
    const block = await screen.findByTestId("attention-block");
    expect(block).toHaveAttribute("data-state", "in_flight");
    expect(screen.getByText("dev")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("terminal: renders the outcome card with a PR link on success", async () => {
    withBaseHandlers();
    server.use(
      http.get("/api/pipelines/runs/overview", () =>
        HttpResponse.json({
          status: "terminal",
          outcome: { state: "completed", pr_url: "https://x/y/pull/42", failure_reason: null },
        }),
      ),
    );
    render(wrap(<TicketDetailPage />));
    const block = await screen.findByTestId("attention-block");
    expect(block).toHaveAttribute("data-state", "completed");
    expect(screen.getByRole("link", { name: /View PR/ })).toHaveAttribute(
      "href",
      "https://x/y/pull/42",
    );
  });

  it("terminal: renders the mono failure_reason on a failed run", async () => {
    withBaseHandlers();
    server.use(
      http.get("/api/pipelines/runs/overview", () =>
        HttpResponse.json({
          status: "terminal",
          outcome: {
            state: "failed",
            pr_url: null,
            failure_reason: "SkillReturn schema violation",
          },
        }),
      ),
    );
    render(wrap(<TicketDetailPage />));
    await screen.findByTestId("attention-block");
    expect(screen.getByText("SkillReturn schema violation")).toBeInTheDocument();
  });

  it("switches to the Runs tab and renders a run card", async () => {
    withBaseHandlers();
    server.use(http.get("/api/pipelines/runs/overview", () => HttpResponse.json(PAUSED_OVERVIEW)));
    render(wrap(<TicketDetailPage />));
    await screen.findByTestId("attention-block");
    await userEvent.click(screen.getByTestId("ticket-tab-runs"));
    expect(await screen.findByTestId("run-card-run-1")).toBeInTheDocument();
    expect(screen.getByTestId("stage-row-write-spec")).toBeInTheDocument();
  });
});
