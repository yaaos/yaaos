/**
 * OAuth user-connection e2e: device-auth connect and disconnect flow.
 *
 * Uses the fake-oauth-provider peer (port 58086) that implements RFC-8628
 * device-authorization. The `oauth_test` plugin registers a `"test"`
 * device-code provider pointed at it — the generic device-code flow (start,
 * poll, grant, connect/disconnect) stays e2e-tested even though no
 * production plugin registers one today. The spec drives the full browser
 * flow:
 *   1. Member navigates to User Details.
 *   2. Clicks "Connect" on the Test Provider card.
 *   3. Dialog shows verification URL + one-time user code.
 *   4. Test runner calls /__test/grant on fake-oauth-provider.
 *   5. Polling closes the dialog; card flips to "Connected".
 *   6. Clicking "Disconnect" + confirming returns the card to "Connect".
 */

import { expect, test } from "@playwright/test";

const BASE = process.env.YAAOS_BASE_URL ?? "http://localhost:58080";
const FAKE_OAUTH_PROVIDER_URL = process.env.FAKE_OAUTH_PROVIDER_URL ?? "http://localhost:58086";

test.describe("OAuth user connections", () => {
  test.beforeEach(async ({ request }) => {
    // Reset yaaos DB and fake-oauth-provider in-memory state.
    await Promise.all([
      request.post(`${BASE}/api/testing/reset`),
      request.post(`${FAKE_OAUTH_PROVIDER_URL}/__test/reset`),
    ]);

    // Seed the owner + stage the test-provider profile.
    await request.post(`${BASE}/api/testing/seed/bootstrap_owner`, {
      data: {
        email: "owner@yaaos.test",
        github_id: "1001",
        org_slug: "acme",
        display_name: "Owner",
        provider: "test",
      },
    });
    await request.post(`${BASE}/api/testing/oauth_test/stage_profile`, {
      data: {
        external_subject: "1001",
        primary_email: "owner@yaaos.test",
        email_verified: true,
        display_name: "Owner",
      },
    });
  });

  test("Connect via device-auth, then Disconnect", async ({ page, request }) => {
    // Log in.
    await page.goto(`${BASE}/login`);
    await page.getByTestId("login-test").click();
    await page.waitForURL(/\/org\/acme\/workspaces$/);

    // Navigate to User Details.
    await page.goto(`${BASE}/org/acme/user/details`);
    await page.waitForURL(/\/org\/acme\/user\/details$/);

    // Wait for the connections section to render.
    await expect(page.getByTestId("connections-section")).toBeVisible({ timeout: 10_000 });

    // The Test Provider connection card should start as "not connected".
    const connectBtn = page.getByTestId("connection-connect-test");
    await expect(connectBtn).toBeVisible();

    // Click Connect — fires the device-auth start.
    await connectBtn.click();

    // Dialog should appear with the fake user code.
    const dialog = page.getByTestId("device-auth-dialog");
    await expect(dialog).toBeVisible({ timeout: 10_000 });

    const codeEl = page.getByTestId("device-auth-user-code");
    await expect(codeEl).toBeVisible();
    // fake-oauth-provider always returns "TEST-1234" as the user_code.
    await expect(codeEl).toContainText("TEST-1234");

    const urlEl = page.getByTestId("device-auth-verification-url");
    await expect(urlEl).toBeVisible();

    // Grant the device auth from the fake-oauth-provider side — next poll will succeed.
    const grantResp = await request.post(`${FAKE_OAUTH_PROVIDER_URL}/__test/grant`);
    expect(grantResp.ok()).toBeTruthy();

    // Wait for polling to pick up the grant and close the dialog.
    await expect(dialog).not.toBeVisible({ timeout: 30_000 });

    // Card should now show "Connected" state with a Disconnect button.
    const disconnectBtn = page.getByTestId("connection-disconnect-test");
    await expect(disconnectBtn).toBeVisible({ timeout: 10_000 });

    // Verify the external_account_id is shown (fake-oauth-provider mints a JWT
    // with sub="fake-oauth-account-id" which becomes the account ID via
    // `account_id_extractor`).
    const connectionRow = page.getByTestId("connection-row-test");
    await expect(connectionRow).toContainText("Connected");

    // Disconnect.
    await disconnectBtn.click();

    // Confirm dialog.
    const confirmBtn = page.getByTestId("disconnect-confirm-action");
    await expect(confirmBtn).toBeVisible({ timeout: 5_000 });
    await confirmBtn.click();

    // Card should flip back to "Connect".
    await expect(page.getByTestId("connection-connect-test")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("connection-disconnect-test")).not.toBeVisible();
  });
});
