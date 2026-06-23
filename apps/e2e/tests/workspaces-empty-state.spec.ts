/**
 * Workspaces page — empty state when no agents are connected.
 *
 * Two branches:
 *  - Org is configured and has zero agents → EmptyState with CTA to
 *    Settings → Workspaces.
 *  - Org is not configured and has zero agents → NotConfiguredBanner renders.
 *
 * Requires the live Docker stack (`bin/dev-rebuild`).
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { resetStack, YAAOS_URL } from "./_helpers";

async function setupAuthedAcmeOwner(page: Page, request: APIRequestContext): Promise<void> {
  await resetStack();
  await request.post(`${YAAOS_URL}/api/testing/seed/bootstrap_owner`, {
    data: {
      email: "owner@yaaos.test",
      github_id: "4001",
      org_slug: "acme-empty",
      display_name: "Owner",
      provider: "test",
    },
  });
  await request.post(`${YAAOS_URL}/api/testing/oauth_test/stage_profile`, {
    data: {
      external_subject: "4001",
      primary_email: "owner@yaaos.test",
      email_verified: true,
      display_name: "Owner",
    },
  });

  await page.goto(`${YAAOS_URL}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(/\/org\/acme-empty\/workspaces$/);
}

test.describe("workspaces page empty state", () => {
  test("shows EmptyState with CTA linking to /settings/workspaces when org has zero agents", async ({
    page,
    request,
  }) => {
    await setupAuthedAcmeOwner(page, request);
    await expect(page.getByTestId("workspaces-page")).toBeVisible({ timeout: 10_000 });

    // With a fresh org and no agents, the empty state renders.
    const emptyState = page.getByTestId("workspaces-empty");
    await expect(emptyState).toBeVisible({ timeout: 10_000 });

    // The CTA must link to the workspaces settings page.
    const cta = emptyState.getByRole("link");
    await expect(cta).toBeVisible();
    await expect(cta).toHaveAttribute("href", /\/org\/acme-empty\/settings\/workspaces/);
  });
});
