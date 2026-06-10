/**
 * App router — composition root that wires domain pages into the route tree.
 *
 * This file lives at the src root (outside any layer) so it can import from
 * both @core/* and @domain/* without violating the layer/domain direction rules.
 * `core/routing` owns the route-shape infrastructure (search schemas, path
 * constants); this file owns the page bindings.
 */

import { AppShell } from "@core/layout/public/app-shell";
import { lessonsSearchSchema, ticketsSearchSchema } from "@core/routing/public/schemas";
import { LoginPage } from "@domain/auth/public/LoginPage";
import { DashboardPage } from "@domain/dashboard/public/index";
import { LessonsPage } from "@domain/lessons/public/index";
import { NotificationsPage } from "@domain/notifications/public/index";
import { AuditSettingsPage } from "@domain/org_settings/public/AuditSettingsPage";
import { AuthSettingsPage } from "@domain/org_settings/public/AuthSettingsPage";
import { MembersSettingsPage } from "@domain/org_settings/public/MembersSettingsPage";
import { WorkspacesSettingsPage } from "@domain/org_settings/public/WorkspacesSettingsPage";
import { BYOKSettingsPage } from "@domain/org_settings/public/byok/BYOKSettingsPage";
import { CodingAgentSettingsPage } from "@domain/org_settings/public/coding_agents/CodingAgentSettingsPage";
import { CodingAgentsSettingsPage } from "@domain/org_settings/public/coding_agents/CodingAgentsSettingsPage";
import { IntegrationsSettingsPage } from "@domain/org_settings/public/integrations/IntegrationsSettingsPage";
import { VcsSettingsPage } from "@domain/org_settings/public/vcs/VcsSettingsPage";
import { OrgPickerPage } from "@domain/orgs/public/OrgPickerPage";
import { TicketDetailPage } from "@domain/tickets/public/TicketDetailPage";
import { TicketsListPage as TicketsPage } from "@domain/tickets/public/TicketsListPage";
import { DetailsPage } from "@domain/user/public/DetailsPage";
import { SecurityPage } from "@domain/user/public/SecurityPage";
import { createRootRoute, createRoute, createRouter, redirect } from "@tanstack/react-router";

const rootRoute = createRootRoute({ component: AppShell });

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: async () => {
    // Probe `/api/auth/me`. 401 → login. 200 with exactly one membership →
    // that org's dashboard (sole option, no picker needed). 200 with zero
    // or multiple → the picker. The server has no "current org" concept;
    // picking is explicit, by the user.
    const r = await fetch("/api/auth/me", { credentials: "include" });
    if (r.status === 401) throw redirect({ to: "/login" });
    if (r.ok) {
      const body = (await r.json()) as { memberships: { slug: string }[] };
      const only = body.memberships.length === 1 ? body.memberships[0] : null;
      if (only) {
        throw redirect({
          to: "/orgs/$slug/dashboard",
          params: { slug: only.slug },
        });
      }
      // 0 → empty-state picker; >1 → user must pick.
      throw redirect({ to: "/orgs" });
    }
    throw redirect({ to: "/login" });
  },
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage,
  // Guard against bouncing back to /login when the user already has a
  // valid session — that's the loop that produces "frozen spinner" when
  // someone navigates here from a deep link with a live cookie.
  beforeLoad: async () => {
    const r = await fetch("/api/auth/me", { credentials: "include" });
    if (r.ok) throw redirect({ to: "/" });
  },
});

const orgsPickerRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/orgs",
  component: OrgPickerPage,
});

const orgScopeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/orgs/$slug",
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
  validateSearch: ticketsSearchSchema,
});

const orgTicketDetailRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/tickets/$ticketId",
  component: TicketDetailPage,
});

const orgLessonsRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/lessons",
  component: LessonsPage,
  validateSearch: lessonsSearchSchema,
});

// /orgs/$slug/settings → /orgs/$slug/settings/auth. The shell + per-tab
// pages live under /settings/{section}; the bare /settings path redirects so
// older bookmarks don't 404 silently.
const orgSettingsIndexRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings",
  beforeLoad: ({ params }) => {
    throw redirect({
      to: "/orgs/$slug/settings/auth",
      params: { slug: params.slug },
    });
  },
});

const orgSettingsAuthRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings/auth",
  component: AuthSettingsPage,
});

const orgSettingsMembersRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings/members",
  component: MembersSettingsPage,
});

const orgSettingsAuditRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings/audit",
  component: AuditSettingsPage,
});

const orgSettingsVcsRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings/vcs",
  component: VcsSettingsPage,
});

const orgSettingsCodingAgentsRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings/coding-agents",
  component: CodingAgentsSettingsPage,
});

const orgSettingsCodingAgentDetailRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings/coding-agents/$pluginId",
  component: function CodingAgentDetailRoute() {
    const { pluginId } = orgSettingsCodingAgentDetailRoute.useParams();
    return <CodingAgentSettingsPage pluginId={pluginId} />;
  },
});

const orgSettingsByokRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings/api-keys",
  component: BYOKSettingsPage,
});

const orgSettingsIntegrationsRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings/mcp-proxy",
  component: IntegrationsSettingsPage,
});

const orgSettingsWorkspacesRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings/workspaces",
  component: WorkspacesSettingsPage,
});

// User-area pages nest under the current org so the URL alone carries
// all routing context (no module-global current-org, no localStorage).
// The backend routes they call (`/api/user/*`, `/api/notifications/*`)
// stay USER_SCOPED and ignore `X-Yaaos-Org-Slug`; the slug in the path is
// purely a frontend routing concern.
const orgUserRedirectRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/user",
  beforeLoad: ({ params }) => {
    throw redirect({
      to: "/orgs/$slug/user/details",
      params: { slug: params.slug },
    });
  },
});

const orgUserDetailsRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/user/details",
  component: DetailsPage,
});

const orgUserSecurityRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/user/security",
  component: SecurityPage,
});

const orgUserNotificationsRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/user/notifications",
  component: NotificationsPage,
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  orgsPickerRoute,
  orgScopeRoute.addChildren([
    orgIndexRoute,
    orgDashboardRoute,
    orgTicketsRoute,
    orgTicketDetailRoute,
    orgLessonsRoute,
    orgSettingsIndexRoute,
    orgSettingsAuthRoute,
    orgSettingsMembersRoute,
    orgSettingsAuditRoute,
    orgSettingsVcsRoute,
    orgSettingsCodingAgentsRoute,
    orgSettingsCodingAgentDetailRoute,
    orgSettingsByokRoute,
    orgSettingsIntegrationsRoute,
    orgSettingsWorkspacesRoute,
    orgUserRedirectRoute,
    orgUserDetailsRoute,
    orgUserSecurityRoute,
    orgUserNotificationsRoute,
  ]),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
