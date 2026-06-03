import { useOtelIdentitySync } from "@core/observability";
import { Sidebar } from "@core/sidebar";
import { useServerEvents } from "@core/sse";
import { Outlet, useRouterState } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import { BrokenIntegrationsBanner } from "./broken-integrations-banner";

// Two routes render outside the shell: `/login` (anonymous, no nav) and
// `/orgs` (the picker — explicit "no org selected" surface). Every other
// authenticated route lives under `/orgs/$slug/...` and gets the sidebar.
const STANDALONE_PATHS = new Set(["/login", "/orgs"]);

export function AppShell() {
  // Owns the browser-wide general-event SSE stream; (re)targets it at the
  // active org as the route changes. Called unconditionally (before the
  // standalone-path early return) to respect the rules-of-hooks.
  useServerEvents();
  // Syncs authenticated user identity into the OTel holder so spans carry
  // yaaos.org_id / yaaos.user_id. Called unconditionally per rules-of-hooks.
  useOtelIdentitySync();

  const { location } = useRouterState();
  const pathname = location.pathname;
  const mainRef = useRef<HTMLElement | null>(null);

  // On every route change, move keyboard focus to the first heading in <main>
  // (if one exists) or to <main> itself so screen-reader and keyboard users
  // land at the top of the new page rather than wherever focus was before.
  // tabIndex={-1} on <main> makes it programmatically focusable without
  // adding it to the tab order. `pathname` is declared as used so the linter
  // sees the intent; the real side-effect is the focus call.
  useEffect(() => {
    if (!mainRef.current || !pathname) return;
    const h1 = mainRef.current.querySelector<HTMLElement>("h1");
    const target = h1 ?? mainRef.current;
    target.focus({ preventScroll: false });
  }, [pathname]);

  if (STANDALONE_PATHS.has(pathname)) {
    return (
      <div className="h-screen w-screen overflow-y-auto">
        <Outlet />
      </div>
    );
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar />
      <div className="flex flex-col flex-1 min-w-0">
        <BrokenIntegrationsBanner />
        {/* tabIndex={-1} makes <main> programmatically focusable for focus-reset
            without placing it in the natural tab order. */}
        <main ref={mainRef} tabIndex={-1} className="flex-1 overflow-y-auto p-6 outline-none">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
