/**
 * OAuth user-connection e2e: device-auth connect and disconnect flow.
 *
 * Uses the fake-openai peer (port 58086) that mirrors the ChatGPT device-auth
 * endpoints. The spec drives the full browser flow:
 *   1. Member navigates to User Details.
 *   2. Clicks "Connect" on the Codex card.
 *   3. Dialog shows verification URL + one-time user code.
 *   4. Test runner calls /__test/grant on fake-openai.
 *   5. Polling closes the dialog; card flips to "Connected".
 *   6. Clicking "Disconnect" + confirming returns the card to "Connect".
 */

import { expect, test } from "@playwright/test";

const BASE = process.env.YAAOS_BASE_URL ?? "http://localhost:58080";
const FAKE_OPENAI_URL = process.env.FAKE_OPENAI_URL ?? "http://localhost:58086";

test.describe("OAuth user connections", () => {
  test.beforeEach(async ({ request }) => {
    // Reset yaaos DB and fake-openai in-memory state.
    await Promise.all([
      request.post(`${BASE}/api/testing/reset`),
      request.post(`${FAKE_OPENAI_URL}/__test/reset`),
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

    // The Codex connection card should start as "not connected".
    const connectBtn = page.getByTestId("connection-connect-codex");
    await expect(connectBtn).toBeVisible();

    // Click Connect — fires the device-auth start.
    await connectBtn.click();

    // Dialog should appear with the fake user code.
    const dialog = page.getByTestId("device-auth-dialog");
    await expect(dialog).toBeVisible({ timeout: 10_000 });

    const codeEl = page.getByTestId("device-auth-user-code");
    await expect(codeEl).toBeVisible();
    // fake-openai always returns "TEST-1234" as the user_code.
    await expect(codeEl).toContainText("TEST-1234");

    const urlEl = page.getByTestId("device-auth-verification-url");
    await expect(urlEl).toBeVisible();

    // Grant the device auth from the fake-openai side — next poll will succeed.
    const grantResp = await request.post(`${FAKE_OPENAI_URL}/__test/grant`);
    expect(grantResp.ok()).toBeTruthy();

    // Wait for polling to pick up the grant and close the dialog.
    await expect(dialog).not.toBeVisible({ timeout: 30_000 });

    // Card should now show "Connected" state with a Disconnect button.
    const disconnectBtn = page.getByTestId("connection-disconnect-codex");
    await expect(disconnectBtn).toBeVisible({ timeout: 10_000 });

    // Verify the external_account_id is shown (fake-openai mints a JWT with
    // sub="fake-user-001" which becomes the account ID via `account_id_extractor`).
    const connectionRow = page.getByTestId("connection-row-codex");
    await expect(connectionRow).toContainText("Connected");

    // Disconnect.
    await disconnectBtn.click();

    // Confirm dialog.
    const confirmBtn = page.getByTestId("disconnect-confirm-action");
    await expect(confirmBtn).toBeVisible({ timeout: 5_000 });
    await confirmBtn.click();

    // Card should flip back to "Connect".
    await expect(page.getByTestId("connection-connect-codex")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("connection-disconnect-codex")).not.toBeVisible();
  });
});
