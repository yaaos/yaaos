/**
 * Login + membership end-to-end: login via `oauth_test` → land on dashboard → invite
 * member → accept invite → change role → logout-all.
 *
 * Drives the real backend, using `/api/testing/*` helpers to reset the DB
 * and seed the bootstrap user. The `oauth_test` provider is the path we
 * exercise because it short-circuits GitHub's redirect.
 */

import { expect, test } from "@playwright/test";

const BASE = process.env.YAAOS_BASE_URL ?? "http://localhost:58080";

test.describe("auth + members", () => {
  test("login → invite → accept → change role → logout-all", async ({ page, request }) => {
    // Reset + seed: bootstrap creates owner@yaaos.test in the `acme` org.
    await request.post(`${BASE}/api/testing/reset`);
    await request.post(`${BASE}/api/testing/seed/bootstrap_owner`, {
      data: {
        email: "owner@yaaos.test",
        github_id: "1001",
        org_slug: "acme",
        display_name: "Owner",
        provider: "test",
      },
    });
    // Stage the oauth_test profile that will be returned on callback.
    await request.post(`${BASE}/api/testing/oauth_test/stage_profile`, {
      data: {
        external_subject: "1001",
        primary_email: "owner@yaaos.test",
        email_verified: true,
        display_name: "Owner",
      },
    });

    await page.goto(`${BASE}/login`);
    await page.getByTestId("login-test").click();
    await page.waitForURL(/\/orgs\/acme\/dashboard$/);

    // Regression: SPA-internal nav → user details → dashboard click must
    // land on /orgs/acme/dashboard, not "Not Found".
    await page.getByTestId("user-card-button").click();
    await page.getByTestId("user-nav-details").click();
    await page.waitForURL(/\/orgs\/acme\/user\/details$/);
    await page.getByTestId("nav-dashboard").click();
    await page.waitForURL(/\/orgs\/acme\/dashboard$/);

    // Regression: HARD-NAV directly to /orgs/acme/user/details (no SPA
    // history), then click Dashboard — must still land on /orgs/acme/dashboard.
    // Invariant: the sidebar must read the active org slug from the URL on
    // every render. A module-global slug cache that's null on first load
    // would make the sidebar build bare `/dashboard` hrefs → NotFound.
    await page.goto(`${BASE}/orgs/acme/user/details`);
    await page.waitForURL(/\/orgs\/acme\/user\/details$/);
    await page.getByTestId("nav-dashboard").click();
    await page.waitForURL(/\/orgs\/acme\/dashboard$/);

    // Members page: invite a new member.
    await page.goto(`${BASE}/orgs/acme/settings/members`);
    await page.locator('input[type="email"]').fill("bob@example.com");
    // The shadcn Select is a Radix popover, not a native <select>. Open
    // it and click the option in the floating listbox.
    await page.getByTestId("invite-role").click();
    await page.getByRole("option", { name: "builder" }).click();
    await page.getByRole("button", { name: "Invite" }).click();
    // Wait for the network roundtrip (mutation + reload) before pulling the
    // test inbox, otherwise the invite-send may not have hit SMTP yet.
    await page.waitForResponse(
      (resp) => resp.url().includes("/api/memberships/invite") && resp.status() === 200,
      { timeout: 10_000 },
    );

    // Fetch the raw invitation token from the test inbox.
    const inboxResp = await request.get(`${BASE}/api/testing/email_inbox`);
    const inbox: { messages: { to: string; body: string }[] } = await inboxResp.json();
    const msg = inbox.messages.find((m) => m.to === "bob@example.com");
    expect(msg, "invite email captured").toBeTruthy();
    const tokenMatch = msg!.body.match(/token=([^\s]+)/);
    expect(tokenMatch).toBeTruthy();
    const token = tokenMatch![1];

    // Accept the invite as Bob — seed his user + session first.
    await request.post(`${BASE}/api/testing/seed/user_with_session`, {
      data: { email: "bob@example.com", session_cookie: "bob-test-cookie" },
    });
    const acceptResp = await request.post(`${BASE}/api/memberships/accept`, {
      data: { token },
      headers: { cookie: "yaaos_session=bob-test-cookie" },
    });
    expect(acceptResp.status()).toBe(200);

    // Owner promotes Bob to admin.
    await page.reload();
    await page.getByTestId("role-bob").click();
    await page.getByRole("option", { name: "admin" }).click();

    // Sign out of every session. Action moved to the Security page.
    await page.goto(`${BASE}/orgs/acme/user/security`);
    await page.getByTestId("logout-all").click();
    await page.waitForURL(/\/login$/);
  });
});
