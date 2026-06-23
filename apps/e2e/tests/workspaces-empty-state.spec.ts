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
import { resetStack, seedGithubInstall, YAAOS_URL } from "./_helpers";

/**
 * Seed "acme-empty": configured org (GitHub install + BYOK + workspace ARN),
 * but with an IAM ARN that the test-agent Docker container will never match.
 * This prevents the container from registering, keeping `agents.length === 0`
 * reliably throughout the test.
 */
async function setupAuthedAcmeOwner(
  page: Page,
  request: APIRequestContext,
): Promise<void> {
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
  // Override the IAM ARN to a value the test-agent container's mock-aws will
  // never return.  The workspace requirement only checks non-null, so the org
  // is still "configured" — but the agent's STS exchange finds no matching org
  // and cannot register.
  await request.post(`${YAAOS_URL}/api/testing/seed/org_iam_arn`, {
    data: {
      org_slug: "acme-empty",
      iam_arn: "arn:aws:iam::999999999999:role/not-a-test-agent-role",
    },
  });
  // GitHub install + BYOK makes the org fully configured.
  await seedGithubInstall({ targetOrgSlug: "acme-empty" });

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

    // With a configured org and no agents, the empty state renders.
    const emptyState = page.getByTestId("workspaces-empty");
    await expect(emptyState).toBeVisible({ timeout: 10_000 });

    // The CTA must link to the workspaces settings page.
    const cta = emptyState.getByRole("link");
    await expect(cta).toBeVisible();
    await expect(cta).toHaveAttribute("href", /\/org\/acme-empty\/settings\/workspaces/);
  });
});
