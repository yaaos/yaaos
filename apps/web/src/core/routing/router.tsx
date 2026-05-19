import { setCurrentOrgSlug } from "@core/api";
import { AppShell } from "@core/layout";
import { AccountPage, LoginPage } from "@domain/auth";
import { DashboardPage } from "@domain/dashboard";
import { MemoryPage } from "@domain/memory";
import { AuditPage, MembersPage, SsoConfigPage } from "@domain/orgs";
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
    // Stale URLs from earlier failed-login flows can leave the literal
    // string "undefined" in the slug. Bounce through / so indexRoute
    // re-probes /me and picks the right org.
    if (!params.slug || params.slug === "undefined" || params.slug === "null") {
      throw redirect({ to: "/" });
    }
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

const orgAuditRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/audit",
  component: AuditPage,
});

const orgSsoRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/sso",
  component: SsoConfigPage,
});

// Legacy aliases — M01-era links + e2e specs target `/dashboard`,
// `/tickets`, `/memory`, `/settings`. Render the same components directly
// (no auth probe) so M01 flows keep working. M02 flows go through
// `/orgs/$slug/...`.
const legacyDashboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/dashboard",
  component: DashboardPage,
});

const legacyTicketsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tickets",
  component: TicketsPage,
});

const legacyTicketDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tickets/$ticketId",
  component: TicketDetailPage,
});

const legacyMemoryRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/memory",
  component: MemoryPage,
});

const legacySettingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: SettingsPage,
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  accountRoute,
  legacyDashboardRoute,
  legacyTicketsRoute,
  legacyTicketDetailRoute,
  legacyMemoryRoute,
  legacySettingsRoute,
  orgScopeRoute.addChildren([
    orgIndexRoute,
    orgDashboardRoute,
    orgTicketsRoute,
    orgTicketDetailRoute,
    orgMemoryRoute,
    orgSettingsRoute,
    orgMembersRoute,
    orgAuditRoute,
    orgSsoRoute,
  ]),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
