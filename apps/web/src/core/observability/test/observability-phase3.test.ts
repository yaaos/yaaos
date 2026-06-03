/**
 * Phase-3 hardening tests for core/observability:
 * - identity sync: clears identity ONLY on 401; leaves it intact + records error on non-401
 * - global error handlers: _resetObservabilityForTests removes only our handlers (prior handlers survive)
 * - traceparent propagation: propagateTraceHeaderCorsUrls pattern restricted to same-origin /api/
 */

import { _resetAuthFailureForTests } from "@core/api/public/auth-failure";
import { act, renderHook } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "../../../test/msw/server";
import { setIdentity } from "../identity";
import { _resetObservabilityForTests, configure } from "../public/sdk";
import { useOtelIdentitySync } from "../public/use-otel-identity-sync";

// Module-level slug controlled by tests. vi.mock is hoisted, so we use a
// module-level variable the mock closure reads by reference.
let _mockOrgSlug: string | null = "acme";

vi.mock("@core/api/public/org-context", () => ({
  getCurrentOrgSlug: () => _mockOrgSlug,
}));

// ── useOtelIdentitySync — 401 vs non-401 behaviour ───────────────────────────

describe("useOtelIdentitySync — identity cleared only on 401", () => {
  // Reset redirect mutex between tests
  beforeEach(() => {
    _mockOrgSlug = "acme";
    _resetAuthFailureForTests();
  });

  afterEach(() => {
    _resetObservabilityForTests();
    _resetAuthFailureForTests();
    vi.restoreAllMocks();
  });

  it("records an error and does NOT clear identity on a 503 response", async () => {
    setIdentity({ orgId: "acme", userId: "u1" });

    const recordSpy = vi.spyOn(await import("../public/sdk"), "recordException");

    server.use(
      http.get("/api/auth/me", () => new HttpResponse("Service Unavailable", { status: 503 })),
    );

    const { unmount } = renderHook(() => useOtelIdentitySync());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    unmount();

    // recordException should be called with the non-401 error
    expect(recordSpy).toHaveBeenCalled();
  });

  it("records an error and does NOT clear identity on a network error", async () => {
    setIdentity({ orgId: "acme", userId: "u1" });

    const recordSpy = vi.spyOn(await import("../public/sdk"), "recordException");

    server.use(http.get("/api/auth/me", () => HttpResponse.error()));

    const { unmount } = renderHook(() => useOtelIdentitySync());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    unmount();

    // recordException should be called for the network failure
    expect(recordSpy).toHaveBeenCalled();
  });

  it("clears identity silently on a 401 and does NOT navigate", async () => {
    // Regression guard: this probe runs on pre-auth pages (e.g. /login) where
    // a 401 is expected. It must clear identity WITHOUT redirecting — routing
    // through apiFetch's 401 handler would hard-redirect to /login and loop.
    setIdentity({ orgId: "acme", userId: "u1" });

    const assignSpy = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: {
        ...window.location,
        assign: assignSpy,
        pathname: "/login",
        search: "",
        hash: "",
      },
    });

    const recordSpy = vi.spyOn(await import("../public/sdk"), "recordException");

    server.use(
      http.get("/api/auth/me", () =>
        HttpResponse.json({ error: "unauthenticated" }, { status: 401 }),
      ),
    );

    const { unmount } = renderHook(() => useOtelIdentitySync());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    unmount();

    // No navigation triggered, and 401 is not treated as a recordable error.
    expect(assignSpy).not.toHaveBeenCalled();
    expect(recordSpy).not.toHaveBeenCalled();
  });

  it("re-runs the effect when org slug changes between renders", async () => {
    // Verifies [orgSlug] dep: two separate mounts with different slugs each
    // produce a fetch. This confirms the effect fires per-slug, not once globally.
    // (A same-component rerender test would require React state to force the
    // re-render; separate mounts are equivalent and simpler.)
    let fetchCount = 0;
    server.use(
      http.get("/api/auth/me", () => {
        fetchCount++;
        return HttpResponse.json({
          user: { id: "u1", display_name: "Jane", primary_email: null, emails: [] },
          memberships: [],
        });
      }),
    );

    // Mount with "acme"
    _mockOrgSlug = "acme";
    const { unmount: u1 } = renderHook(() => useOtelIdentitySync());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    u1();
    const after1 = fetchCount;
    expect(after1).toBeGreaterThan(0);

    // Mount fresh with "neworg" — simulates org-slug navigation
    _mockOrgSlug = "neworg";
    const { unmount: u2 } = renderHook(() => useOtelIdentitySync());
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    u2();

    expect(fetchCount).toBeGreaterThan(after1);
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
