import { Outlet, useRouterState } from "@tanstack/react-router";
import { Sidebar } from "./sidebar";
import { Topbar } from "./topbar";

const CRUMB_BY_PATH: Record<string, string> = {
  "/dashboard": "Dashboard",
  "/tickets": "Tickets",
  "/memory": "Memory",
  "/settings": "Settings",
};

export function AppShell() {
  const { location } = useRouterState();
  const crumb = CRUMB_BY_PATH[location.pathname] ?? "yaaos";

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar />
      <div className="flex flex-col flex-1 min-w-0">
        <Topbar crumb={crumb} />
        <main className="flex-1 overflow-y-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
