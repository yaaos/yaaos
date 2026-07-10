import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type React from "react";
import { describe, expect, it } from "vitest";
import { server } from "../../../test/msw/server";
import { RunsTab } from "../runs";

/**
 * Component tests for the Runs tab's "Re-run" action on a terminal run
 * card — renders for failed/cancelled/killed runs, absent for
 * completed/running, and fires `POST /api/pipelines/runs/{id}/rerun` after
 * confirmation. Pattern mirrors `ticket-detail.test.tsx`.
 */

function baseRun(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: "run-1",
    pipeline_name: "dev",
    state: "failed",
    kickoff: {
      intake_point_id: "test",
      actor_kind: "user",
      actor_login: "alice",
      input_text: null,
    },
    created_at: "2026-05-23T00:00:00Z",
    completed_at: "2026-05-23T00:01:00Z",
    failure_reason: "boom",
    stages: [],
    ...overrides,
  };
}

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

describe("RunsTab re-run action (MSW)", () => {
  it("renders the Re-run button for a failed run", async () => {
    server.use(http.get("/api/pipelines/runs", () => HttpResponse.json({ runs: [baseRun()] })));
    render(wrap(<RunsTab ticketId="t1" />));
    expect(await screen.findByTestId("rerun-run")).toBeInTheDocument();
  });

  it.each(["cancelled", "killed"])("renders the Re-run button for a %s run", async (state) => {
    server.use(
      http.get("/api/pipelines/runs", () => HttpResponse.json({ runs: [baseRun({ state })] })),
    );
    render(wrap(<RunsTab ticketId="t1" />));
    expect(await screen.findByTestId("rerun-run")).toBeInTheDocument();
  });

  it.each(["completed", "running"])("does not render Re-run for a %s run", async (state) => {
    server.use(
      http.get("/api/pipelines/runs", () => HttpResponse.json({ runs: [baseRun({ state })] })),
    );
    render(wrap(<RunsTab ticketId="t1" />));
    await screen.findByTestId("run-card-run-1");
    expect(screen.queryByTestId("rerun-run")).not.toBeInTheDocument();
  });

  it("confirm-then-mutation fires the rerun POST", async () => {
    let posted = false;
    server.use(
      http.get("/api/pipelines/runs", () => HttpResponse.json({ runs: [baseRun()] })),
      http.post("/api/pipelines/runs/run-1/rerun", () => {
        posted = true;
        return HttpResponse.json({ run_id: "run-2" }, { status: 201 });
      }),
    );
    render(wrap(<RunsTab ticketId="t1" />));
    await userEvent.click(await screen.findByTestId("rerun-run"));
    expect(await screen.findByText("Re-run pipeline?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Re-run" }));
    await waitFor(() => expect(posted).toBe(true));
  });

  it("clicking Re-run does not toggle the run card's accordion", async () => {
    server.use(http.get("/api/pipelines/runs", () => HttpResponse.json({ runs: [baseRun()] })));
    render(wrap(<RunsTab ticketId="t1" />));
    const card = await screen.findByTestId("run-card-run-1");
    expect(card).toHaveAttribute("open");
    await userEvent.click(screen.getByTestId("rerun-run"));
    expect(card).toHaveAttribute("open");
  });
});
