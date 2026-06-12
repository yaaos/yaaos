/**
 * E2e coverage for three user-visible flows that vitest + service tests
 * can't catch:
 *
 *  1. Broken-integrations banner + deep-link. An Owner whose org has a
 *     `mcp_credentials.last_refresh_status="failed"` row sees the red banner
 *     in the app-shell; clicking it lands on the Integrations settings page
 *     with the broken provider's "Reconnect required" badge visible.
 *
 *  2. Multi-org switching (deferred). A user with memberships in two orgs
 *     switches between them via the sidebar/user-card. Real bug-class —
 *     sidebar items, current-org-slug, banner all need to re-evaluate.
 *     The e2e infra to seed two orgs + memberships under one session is
 *     not in place yet; covered here as a TODO.
 *
 *  3. GitHub username verify (covered indirectly). `users.github_username`
 *     is populated by the regular "Sign in with GitHub" callback now; no
 *     dedicated verify-only endpoint. Asserted in the login e2e spec.
 */

import { expect, test } from "@playwright/test";

const BASE = process.env.YAAOS_BASE_URL ?? "http://localhost:58080";

test.describe("integrations + multi-org", () => {
  test("broken-integrations banner deep-links to Integrations settings", async ({
    page,
    request,
  }) => {
    // Reset + bootstrap owner in the `acme` org + seed a broken MCP credential
    // + stage the oauth_test profile so login flows through.
    await request.post(`${BASE}/api/testing/reset`);
    await request.post(`${BASE}/api/testing/seed/bootstrap_owner`, {
      data: {
        email: "owner@yaaos.test",
        github_id: "2001",
        org_slug: "acme",
        display_name: "Owner",
        provider: "test",
      },
    });
    await request.post(`${BASE}/api/testing/seed/broken_integration`, {
      data: { org_slug: "acme", provider: "linear" },
    });
    await request.post(`${BASE}/api/testing/oauth_test/stage_profile`, {
      data: {
        external_subject: "2001",
        primary_email: "owner@yaaos.test",
        email_verified: true,
        display_name: "Owner",
      },
    });

    // Log in. We end up on the org dashboard.
    await page.goto(`${BASE}/login`);
    await page.getByTestId("login-test").click();
    await page.waitForURL(/\/org\/acme\/dashboard$/);

    // Red banner is visible in the app-shell.
    const banner = page.getByTestId("broken-integrations-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("linear");

    // Click the banner → land on the MCP Proxy settings page (
    // renamed `/settings/integrations` → `/settings/mcp-proxy`).
    await banner.click();
    await page.waitForURL(/\/org\/acme\/settings\/mcp-proxy$/);

    // The broken provider's "Reconnect required" badge is visible.
    await expect(page.getByTestId("badge-linear-broken")).toBeVisible();
  });
});
