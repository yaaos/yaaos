import { http, HttpResponse } from "msw";

export const AGENTS_FIXTURE = [
  {
    id: "a1",
    instance_id: "pod-abc",
    state: "reachable",
    last_heartbeat_at: "2026-05-23T00:00:00Z",
    os: "linux",
    cpu_count: 4,
    memory_bytes: 8589934592,
    claimed_workspace_count: 0,
    version: "1.0.0",
  },
];

export const dashboardHandlers = [
  http.get("/api/orgs/:slug/agents", () => {
    return HttpResponse.json(AGENTS_FIXTURE);
  }),
];
