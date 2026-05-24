/**
 * GitHub App install handshake — full UI-driven round-trip.
 *
 * Asserts the path that `seedGithubInstall` deliberately bypasses: an Owner
 * clicks "Install on GitHub" in VCS settings, the SPA fetches a state-signed
 * redirect URL from `POST /api/github/install/start`, the browser follows
 * fake-github's install-picker stub which 302's back to
 * `/api/github/install_callback`, the callback writes the install row, and
 * the SPA's VCS settings page surfaces the connected state.
 *
 * No credential seed is needed any more — the platform yaaos GitHub App's
 * credentials come from env vars, so the empty-state path is simply
 * "no install row on this org".
 */

import { expect, test } from "@playwright/test";

import { resetStack, YAAOS_URL } from "./_helpers";

test.describe("github install handshake", () => {
  test("Owner clicks Install on GitHub → callback writes install row → connected", async ({
    page,
    request,
  }) => {
    await resetStack();
    await request.post(`${YAAOS_URL}/api/testing/seed/bootstrap_owner`, {
      data: {
        email: "owner@yaaos.test",
        github_id: "3001",
        org_slug: "acme",
        display_name: "Owner",
        provider: "test",
      },
    });
    await request.post(`${YAAOS_URL}/api/testing/oauth_test/stage_profile`, {
      data: {
        external_subject: "3001",
        primary_email: "owner@yaaos.test",
        email_verified: true,
        display_name: "Owner",
      },
    });
    // No install row yet, so VCS settings renders the picker in empty state.
    // The platform GitHub App credentials are configured on the backend via
    // env vars (`YAAOS_GITHUB_APP_SLUG=yaaos-test` matches what fake-github's
    // stubbed `GET /app` returns).

    // Log in as Owner.
    await page.goto(`${YAAOS_URL}/login`);
    await page.getByTestId("login-test").click();
    await page.waitForURL(/\/orgs\/acme\/dashboard$/);

    // Navigate to VCS settings; empty state surfaces the picker.
    await page.goto(`${YAAOS_URL}/orgs/acme/settings/vcs`);
    await expect(page.getByTestId("vcs-picker")).toBeVisible();

    // Click the GitHub picker option. The SPA fires the JSON install/start
    // request and then navigates the browser to the signed redirect URL.
    // fake-github's `/apps/<slug>/installations/new` stub 302's back to
    // yaaos's `/api/github/install_callback`, which writes the install row
    // and 302's to "/". The SPA bounces to the org dashboard.
    await Promise.all([
      page.waitForURL(/\/orgs\/acme\/dashboard$/, { timeout: 15_000 }),
      page.getByTestId("vcs-picker-add-github").click(),
    ]);

    // Return to VCS settings — the GitHub card should now render the
    // connected (healthy) state with the account login fake-github seeded.
    await page.goto(`${YAAOS_URL}/orgs/acme/settings/vcs`);
    await expect(page.getByTestId("vcs-connected")).toBeVisible();
    await expect(page.getByTestId("vcs-github-details")).toBeVisible();
    await expect(page.getByTestId("vcs-github-details")).toContainText("acme-org");
    // The "needs setup" badge must not be visible — the handshake completed.
    await expect(page.getByTestId("vcs-github-needs-setup")).toHaveCount(0);
  });
});
