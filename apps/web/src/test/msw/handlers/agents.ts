/**
 * MSW handlers for the workspace-agents endpoints used in Vitest tests.
 *
 * Tests import individual handlers and register them via `server.use(...)`.
 * The default shutdown/cancel-shutdown handlers return all-success outcomes;
 * tests that need mixed outcomes override with `server.use(...)` in-test.
 */

import type { AgentRow, CancelShutdownResult, ShutdownResult } from "@core/api/public/queries";
import { http, HttpResponse } from "msw";

export const ACTIVE_AGENTS_FIXTURE: AgentRow[] = [
  {
    id: "a1",
    instance_id: "pod-abc",
    state: "reachable",
    lifecycle: "active",
    last_heartbeat_at: "2026-05-23T00:00:00Z",
    os: "linux",
    cpu_count: 4,
    memory_bytes: 8589934592,
    claimed_workspace_count: 0,
    version: "1.0.0",
  },
  {
    id: "a2",
    instance_id: "pod-def",
    state: "reachable",
    lifecycle: "active",
    last_heartbeat_at: "2026-05-23T00:00:01Z",
    os: "linux",
    cpu_count: 4,
    memory_bytes: 8589934592,
    claimed_workspace_count: 0,
    version: "1.0.0",
  },
];

export const DRAINING_AGENTS_FIXTURE: AgentRow[] = [
  {
    id: "d1",
    instance_id: "pod-ghi",
    state: "reachable",
    lifecycle: "draining",
    last_heartbeat_at: "2026-05-23T00:00:00Z",
    os: "linux",
    cpu_count: 4,
    memory_bytes: 8589934592,
    claimed_workspace_count: 0,
    version: "1.0.0",
  },
];

/** GET /api/orgs/:slug/agents — returns all-success agent list. */
export const getAgentsHandler = http.get("/api/orgs/:slug/agents", () =>
  HttpResponse.json([...ACTIVE_AGENTS_FIXTURE, ...DRAINING_AGENTS_FIXTURE]),
);

/**
 * POST /api/orgs/:slug/agents/shutdown — all submitted IDs succeed (outcome=draining).
 * Override per-test when mixed outcomes are needed.
 */
export const shutdownHandler = http.post("/api/orgs/:slug/agents/shutdown", async ({ request }) => {
  const body = (await request.json()) as { agent_ids: string[] };
  const result: ShutdownResult = {
    results: body.agent_ids.map((agent_id) => ({ agent_id, outcome: "draining" })),
  };
  return HttpResponse.json(result);
});

/**
 * POST /api/orgs/:slug/agents/cancel-shutdown — all submitted IDs succeed (outcome=active).
 * Override per-test when mixed outcomes are needed.
 */
export const cancelShutdownHandler = http.post(
  "/api/orgs/:slug/agents/cancel-shutdown",
  async ({ request }) => {
    const body = (await request.json()) as { agent_ids: string[] };
    const result: CancelShutdownResult = {
      results: body.agent_ids.map((agent_id) => ({ agent_id, outcome: "active" })),
    };
    return HttpResponse.json(result);
  },
);
