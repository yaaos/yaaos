import { Sidebar } from "@core/sidebar";
import { Outlet, useRouterState } from "@tanstack/react-router";
import { BrokenIntegrationsBanner } from "./broken-integrations-banner";

// User-scoped pages render outside the app shell — no sidebar, no chrome,
// no org nav. The login page in particular must not surface dashboard
// links to anonymous visitors.
const STANDALONE_PATHS = new Set(["/login", "/user", "/orgs"]);

export function AppShell() {
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
