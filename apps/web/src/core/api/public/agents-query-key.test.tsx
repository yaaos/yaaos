/**
 * Verifies that useAgents caches under a slug-scoped key: data fetched for one
 * orgSlug must not be served from the cache when a different orgSlug is used.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { describe, expect, it } from "vitest";
import { server } from "../../../test/msw/server";
import { useAgents } from "./queries";

function wrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

describe("useAgents — slug-scoped query key", () => {
  it("does not serve acme's agents from cache when queried under a different slug", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    const acmeAgent = {
      id: "a-acme",
      instance_id: "pod-acme",
      state: "reachable" as const,
      last_heartbeat_at: null,
      os: "linux",
      cpu_count: 2,
      memory_bytes: 4294967296,
      claimed_workspace_count: 0,
      version: "1.0.0",
    };

    server.use(
      http.get("/api/orgs/acme/agents", () => HttpResponse.json([acmeAgent])),
      // beta org returns an empty list — proves no cross-org cache bleed
      http.get("/api/orgs/beta/agents", () => HttpResponse.json([])),
    );

    // Fetch agents for acme.
    const { result: acmeResult } = renderHook(() => useAgents("acme"), {
      wrapper: wrapper(qc),
    });
    await waitFor(() => expect(acmeResult.current.data).toHaveLength(1));
    expect(acmeResult.current.data?.[0]?.id).toBe("a-acme");

    // Fetch agents for beta using the same QueryClient instance.
    // If the key were not slug-scoped, beta would incorrectly receive acme's
    // cached entry instead of hitting the network and getting [].
    const { result: betaResult } = renderHook(() => useAgents("beta"), {
      wrapper: wrapper(qc),
    });
    await waitFor(() => expect(betaResult.current.data).toHaveLength(0));
  });
});
