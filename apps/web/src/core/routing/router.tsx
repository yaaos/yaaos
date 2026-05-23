import { setCurrentOrgSlug } from "@core/api";
import { AppShell } from "@core/layout";
import { DetailsPage, SecurityPage } from "@domain/account";
import { MessagingPage } from "@domain/account/MessagingPage";
import { LoginPage } from "@domain/auth";
import { DashboardPage } from "@domain/dashboard";
import { LessonsPage } from "@domain/lessons";
import { NotificationsPage } from "@domain/notifications";
import {
  AuditSettingsPage,
  AuthSettingsPage,
  BYOKSettingsPage,
  CodingAgentSettingsPage,
  CodingAgentsSettingsPage,
  IntegrationsSettingsPage,
  MembersSettingsPage,
  VcsSettingsPage,
  WorkspaceSettingsPage,
} from "@domain/org_settings";
import { OrgPickerPage } from "@domain/orgs/OrgPickerPage";
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

const accountRedirectRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/user",
  beforeLoad: () => {
    setCurrentOrgSlug(null);
    throw redirect({ to: "/user/details" });
  },
});

const accountDetailsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/user/details",
  component: DetailsPage,
  beforeLoad: () => setCurrentOrgSlug(null),
});

const accountSecurityRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/user/security",
  component: SecurityPage,
  beforeLoad: () => setCurrentOrgSlug(null),
});

// M06 — user-scoped placeholder routes: routing lands in Phase 2 so the
// sidebar links don't 404; full implementations come later.
const userMessagingRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/user/messaging",
  component: MessagingPage,
  beforeLoad: () => setCurrentOrgSlug(null),
});

const notificationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/notifications",
  component: NotificationsPage,
  beforeLoad: () => setCurrentOrgSlug(null),
});

const orgsPickerRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/orgs",
  component: OrgPickerPage,
  beforeLoad: () => setCurrentOrgSlug(null),
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

const orgLessonsRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/lessons",
  component: LessonsPage,
});

// M03: /orgs/$slug/settings → /orgs/$slug/settings/auth. The shell + per-tab
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

const orgSettingsWorkspaceRoute = createRoute({
  getParentRoute: () => orgScopeRoute,
  path: "/settings/workspace",
  component: WorkspaceSettingsPage,
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  loginRoute,
  accountRedirectRoute,
  accountDetailsRoute,
  accountSecurityRoute,
  userMessagingRoute,
  notificationsRoute,
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
    orgSettingsWorkspaceRoute,
  ]),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
