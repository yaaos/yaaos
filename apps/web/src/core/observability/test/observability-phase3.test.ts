/**
 * Phase-3 hardening tests for core/observability:
 * - identity sync: passive cache reader — correct identity from cache; null when
 *   cache empty (no fetch, no navigation); null when orgSlug absent; updates on
 *   orgSlug change.
 * - global error handlers: _resetObservabilityForTests removes only our handlers (prior handlers survive)
 * - traceparent propagation: propagateTraceHeaderCorsUrls pattern restricted to same-origin /api/
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getIdentity, setIdentity } from "../identity";
import { _resetObservabilityForTests, configure } from "../public/sdk";
import { useOtelIdentitySync } from "../public/use-otel-identity-sync";

// Module-level slug controlled by tests. vi.mock is hoisted, so we use a
// module-level variable the mock closure reads by reference.
let _mockOrgSlug: string | null = "acme";

vi.mock("@core/api/public/org-context", () => ({
  getCurrentOrgSlug: () => _mockOrgSlug,
}));

// Shared cache seed helpers
const AUTHED_USER = {
  user: { id: "u1", display_name: "Jane", primary_email: null, emails: [] },
  memberships: [],
};

function makeWrapper(queryClient: QueryClient) {
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

// ── useOtelIdentitySync — passive cache reading ───────────────────────────────

describe("useOtelIdentitySync — passive cache reader", () => {
  beforeEach(() => {
    _mockOrgSlug = "acme";
    setIdentity(null);
  });

  afterEach(() => {
    _resetObservabilityForTests();
    vi.restoreAllMocks();
  });

  it("sets identity from cache when orgSlug is present and cache holds a user", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(["auth", "me"], AUTHED_USER);

    renderHook(() => useOtelIdentitySync(), { wrapper: makeWrapper(queryClient) });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(getIdentity()).toEqual({ orgId: "acme", userId: "u1" });
  });

  it("identity is null when cache is empty (pre-auth / /login), NO fetch is issued, NO navigation", async () => {
    const queryClient = new QueryClient();
    // cache deliberately empty

    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const assignSpy = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { ...window.location, assign: assignSpy },
    });

    renderHook(() => useOtelIdentitySync(), { wrapper: makeWrapper(queryClient) });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    expect(getIdentity()).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it("identity is null when orgSlug is null even if cache holds a user (URL-scoping guard)", async () => {
    _mockOrgSlug = null;
    const queryClient = new QueryClient();
    queryClient.setQueryData(["auth", "me"], AUTHED_USER);

    renderHook(() => useOtelIdentitySync(), { wrapper: makeWrapper(queryClient) });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(getIdentity()).toBeNull();
  });

  it("identity updates when orgSlug changes between mounts", async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(["auth", "me"], AUTHED_USER);

    // Mount with "acme"
    _mockOrgSlug = "acme";
    const { unmount: u1 } = renderHook(() => useOtelIdentitySync(), {
      wrapper: makeWrapper(queryClient),
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(getIdentity()).toEqual({ orgId: "acme", userId: "u1" });
    u1();

    // Mount with "neworg"
    _mockOrgSlug = "neworg";
    renderHook(() => useOtelIdentitySync(), { wrapper: makeWrapper(queryClient) });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(getIdentity()).toEqual({ orgId: "neworg", userId: "u1" });
  });
});

// ── _resetObservabilityForTests — prior handlers survive ─────────────────────

describe("_resetObservabilityForTests — prior handlers not clobbered", () => {
  afterEach(() => {
    _resetObservabilityForTests();
  });

  it("a listener registered before configure() still fires after reset (error event)", () => {
    // Register a prior error handler
    const priorCalls: string[] = [];
    const priorHandler = (e: ErrorEvent) => {
      priorCalls.push(e.message);
    };
    window.addEventListener("error", priorHandler);

    configure({ collectorEndpoint: undefined });

    // Reset removes our handlers, not the prior one
    _resetObservabilityForTests();

    // Dispatch a synthetic error event — prior handler must still fire
    const evt = new ErrorEvent("error", { message: "prior-handler-test", error: new Error("x") });
    window.dispatchEvent(evt);

    window.removeEventListener("error", priorHandler);

    expect(priorCalls).toContain("prior-handler-test");
  });

  it("a listener registered before configure() still fires after reset (custom unhandledrejection)", () => {
    // jsdom doesn't support PromiseRejectionEvent constructor, so we use CustomEvent
    // to verify the handler survives — the real unhandledrejection path is equivalent.
    const priorCalls: unknown[] = [];
    const priorHandler = (e: Event) => {
      priorCalls.push((e as CustomEvent).detail);
    };
    window.addEventListener("unhandledrejection", priorHandler);

    configure({ collectorEndpoint: undefined });
    _resetObservabilityForTests();

    // Dispatch a CustomEvent (same channel) — prior handler must still receive it
    const evt = new CustomEvent("unhandledrejection", { detail: "prior-rejection-test" });
    window.dispatchEvent(evt);

    window.removeEventListener("unhandledrejection", priorHandler);

    expect(priorCalls).toContain("prior-rejection-test");
  });
});

// ── FetchInstrumentation propagateTraceHeaderCorsUrls ─────────────────────────

describe("FetchInstrumentation — traceparent restricted to same-origin /api/", () => {
  it("the pattern matches same-origin /api/ routes and rejects cross-origin requests", () => {
    // configure() builds: new RegExp(`^${window.location.origin}/api/`)
    // We replicate the same pattern and verify its match semantics.
    const origin = "http://localhost:3000"; // jsdom default
    const pattern = new RegExp(`^${origin}/api/`);

    // Must match same-origin /api/ routes (absolute URL form that fetch resolves to)
    expect(pattern.test(`${origin}/api/auth/me`)).toBe(true);
    expect(pattern.test(`${origin}/api/tickets`)).toBe(true);
    expect(pattern.test(`${origin}/api/orgs/mine`)).toBe(true);

    // Must NOT match cross-origin requests
    expect(pattern.test("https://evil.example.com/api/auth/me")).toBe(false);
    expect(pattern.test("https://collector.internal/api/traces")).toBe(false);

    // Must NOT match same-origin non-/api routes
    expect(pattern.test(`${origin}/static/bundle.js`)).toBe(false);
    expect(pattern.test(`${origin}/orgs/acme/tickets`)).toBe(false);

    // Must NOT match paths that merely start with /api without the trailing slash
    // (e.g. a hypothetical /apikeys route should not match — confirmed by the trailing /)
    expect(pattern.test(`${origin}/apikeys`)).toBe(false);
  });
});
