import { useOtelIdentitySync } from "@core/observability/use-otel-identity-sync";
import { Sidebar } from "@core/sidebar";
import { useServerEvents } from "@core/sse";
import { Outlet, useRouterState } from "@tanstack/react-router";
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
        <main className="flex-1 overflow-y-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
