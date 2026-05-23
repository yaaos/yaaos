import { Sidebar } from "@core/sidebar";
import { Outlet, useRouterState } from "@tanstack/react-router";
import { BrokenIntegrationsBanner } from "./broken-integrations-banner";
import { Topbar } from "./topbar";

const CRUMB_BY_PATH: Record<string, string> = {
  "/dashboard": "Dashboard",
  "/tickets": "Tickets",
  "/lessons": "Lessons",
  "/settings": "Settings",
};

// User-scoped pages render outside the app shell — no sidebar, no topbar,
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

  const crumb = CRUMB_BY_PATH[pathname] ?? "yaaos";
  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar />
      <div className="flex flex-col flex-1 min-w-0">
        <Topbar crumb={crumb} />
        <BrokenIntegrationsBanner />
        <main className="flex-1 overflow-y-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
