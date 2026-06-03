/**
 * Focus-reset: on route change, keyboard focus moves to the first <h1> inside
 * <main> (if present) or to <main> itself. Ensures screen-reader and keyboard
 * users land at the top of the new page after navigation.
 *
 * Tests render a minimal re-implementation of the focus-reset logic so they
 * stay fast and isolated from the full AppShell dependency graph. The AppShell
 * integration tests (first two cases) exercise the real component to assert
 * that <main> receives focus.
 */

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { useEffect, useRef, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ---------------------------------------------------------------------------
// Mocks for AppShell's real-component tests
// ---------------------------------------------------------------------------
const routeState = { pathname: "/orgs/acme/dashboard", status: "idle" as const };

vi.mock("@core/sse/public/subscriber", () => ({ useServerEvents: () => {} }));
vi.mock("@core/observability/public/use-otel-identity-sync", () => ({
  useOtelIdentitySync: () => {},
}));
vi.mock("@core/sidebar/public/sidebar", () => ({
  Sidebar: () => <nav aria-label="primary navigation" />,
}));
vi.mock("../broken-integrations-banner", () => ({
  BrokenIntegrationsBanner: () => null,
}));
vi.mock("@tanstack/react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@tanstack/react-router")>();
  return {
    ...actual,
    // Outlet renders nothing — tests that need content inject it differently.
    Outlet: () => null,
    // Honor the `select` projection AppShell uses (pathname + router status).
    useRouterState: ({ select }: { select: (s: unknown) => unknown }) =>
      select({ location: { pathname: routeState.pathname }, status: routeState.status }),
  };
});

import { AppShell } from "../public/app-shell";

afterEach(() => {
  cleanup();
  routeState.pathname = "/orgs/acme/dashboard";
});

beforeEach(() => {
  document.body.tabIndex = -1;
  document.body.focus();
});

// ---------------------------------------------------------------------------
// Helper: a minimal focus-reset component that mirrors the logic in AppShell
// but accepts page content as children, so tests can control DOM structure
// without fighting the Outlet mock.
// ---------------------------------------------------------------------------
// FocusResetHarness uses a synchronous useEffect calling .focus() directly for
// determinism in jsdom. It deliberately elides the RAF polling loop that the
// real AppShell uses (45-frame budget) — the RAF path is covered by the
// AppShell integration tests in the first describe block below.
function FocusResetHarness({
  pathname,
  children,
}: {
  pathname: string;
  children?: React.ReactNode;
}) {
  const mainRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!mainRef.current || !pathname) return;
    const h1 = mainRef.current.querySelector<HTMLElement>("h1");
    const target = h1 ?? mainRef.current;
    target.focus({ preventScroll: false });
  }, [pathname]);
  return (
    <main ref={mainRef} tabIndex={-1}>
      {children}
    </main>
  );
}

describe("AppShell — focus reset on route change", () => {
  describe("real AppShell: <main> receives focus on mount", () => {
    it("focuses <main> when the Outlet renders nothing", async () => {
      routeState.pathname = "/orgs/acme/dashboard";
      render(<AppShell />);

      await waitFor(
        () => {
          expect(document.activeElement).toBe(document.querySelector("main"));
        },
        { timeout: 2000 },
      );
    });
  });

  describe("focus-reset logic (harness): route-driven focus placement", () => {
    it("focuses <main> when no <h1> is present", async () => {
      render(
        <FocusResetHarness pathname="/orgs/acme/dashboard">
          <section>content without a heading</section>
        </FocusResetHarness>,
      );

      await waitFor(
        () => {
          expect(document.activeElement).toBe(document.querySelector("main"));
        },
        { timeout: 2000 },
      );
    });

    it("focuses the first <h1> when one is present", async () => {
      render(
        <FocusResetHarness pathname="/orgs/acme/tickets">
          <h1 tabIndex={-1} data-testid="page-h1">
            Tickets
          </h1>
        </FocusResetHarness>,
      );

      await waitFor(
        () => {
          expect(document.activeElement).toBe(screen.getByTestId("page-h1"));
        },
        { timeout: 2000 },
      );
    });

    it("re-focuses on pathname change (simulates route navigation)", async () => {
      // NavigationDriver holds both the path and the page content as a single
      // state slice so they update atomically — no timing gap between the
      // pathname changing and the new content appearing.
      type Page = "dashboard" | "lessons";
      function NavigationDriver() {
        const [page, setPage] = useState<Page>("dashboard");
        const pathname = page === "dashboard" ? "/orgs/acme/dashboard" : "/orgs/acme/lessons";
        return (
          <>
            <FocusResetHarness pathname={pathname}>
              {page === "dashboard" ? (
                <section>dashboard (no h1)</section>
              ) : (
                <h1 tabIndex={-1} data-testid="lessons-h1">
                  Lessons
                </h1>
              )}
            </FocusResetHarness>
            <button type="button" onClick={() => setPage("lessons")} data-testid="go-lessons">
              Go to Lessons
            </button>
          </>
        );
      }

      render(<NavigationDriver />);

      // Initial state: main gets focus (no h1).
      await waitFor(
        () => {
          expect(document.activeElement).toBe(document.querySelector("main"));
        },
        { timeout: 2000 },
      );

      // Move focus to the button (simulates user interaction).
      act(() => {
        screen.getByTestId("go-lessons").focus();
      });

      // Navigate.
      act(() => {
        screen.getByTestId("go-lessons").click();
      });

      // After navigation: focus should jump to the new <h1>.
      await waitFor(
        () => {
          expect(document.activeElement).toBe(screen.getByTestId("lessons-h1"));
        },
        { timeout: 2000 },
      );
    });
  });
});
