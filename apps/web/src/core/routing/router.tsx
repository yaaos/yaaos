import { setCurrentOrgSlug } from "@core/api";
import { AppShell } from "@core/layout";
import { AccountPage, LoginPage } from "@domain/auth";
import { DashboardPage } from "@domain/dashboard";
import { MemoryPage } from "@domain/memory";
import { MembersPage } from "@domain/orgs";
import { SettingsPage } from "@domain/settings";
import { TicketDetailPage, TicketsPage } from "@domain/tickets";
import { createRootRoute, createRoute, createRouter, redirect } from "@tanstack/react-router";

const rootRoute = createRootRoute({ component: AppShell });

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: async () => {
    // Probe `/api/auth/me`; on 401, send to login. Otherwise pick the first org.
    const r = await fetch("/api/auth/me", { credentials: "include" });
    if (r.status === 401) throw redirect({ to: "/login" });
    if (r.ok) {
      const body = (await r.json()) as { current_org_slug: string | null };
      if (body.current_org_slug) {
        throw redirect({
          to: "/orgs/$slug/dashboard",
          params: { slug: body.current_org_slug },
        });
      }
    }
    throw redirect({ to: "/login" });
  },
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage,
  beforeLoad: () => {
    setCurrentOrgSlug(null);
  },
});

const accountRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/account",
  component: AccountPage,
  beforeLoad: () => {
    // /account is user-scoped — no org context.
    setCurrentOrgSlug(null);
  },
});

const orgScopeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/orgs/$slug",
  beforeLoad: ({ params }) => {
    setCurrentOrgSlug(params.slug);
  },
});

const orgIndexRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/",
  beforeLoad: ({ params }) => {
    throw redirect({ to: "/orgs/$slug/dashboard", params: { slug: params.slug } });
  },
});

const orgDashboardRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/dashboard",
  component: DashboardPage,
});

const orgTicketsRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/tickets",
  component: TicketsPage,
});

const orgTicketDetailRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/tickets/$ticketId",
  component: TicketDetailPage,
});

const orgMemoryRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/memory",
  component: MemoryPage,
});

const orgSettingsRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings",
  component: SettingsPage,
});

const orgMembersRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/members",
  component: MembersPage,
});

// Legacy aliases — kept until every link is updated. Phase 14 deletes these.
const legacyDashboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/dashboard",
  beforeLoad: () => {
    // Best-effort: send to the first org the user is in.
    throw redirect({ to: "/" });
  },
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  accountRoute,
  legacyDashboardRoute,
  orgScopeRoute.addChildren([
    orgIndexRoute,
    orgDashboardRoute,
    orgTicketsRoute,
    orgTicketDetailRoute,
    orgMemoryRoute,
    orgSettingsRoute,
    orgMembersRoute,
  ]),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
