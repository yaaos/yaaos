/**
 * Workspaces page — builder role sees agents but no admin controls.
 *
 * Seeds an org + owner (for setup), then seeds a builder member and logs in
 * as the builder. Verifies that agent cards render without checkboxes or
 * bulk-action buttons.
 *
 * Requires the live Docker stack (`bin/dev-rebuild`).
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { resetStack, YAAOS_URL } from "./_helpers";

const OWNER_GITHUB_ID = "7001";
const BUILDER_GITHUB_ID = "7002";
const ORG_SLUG = "acme-readonly";

async function setupOrgWithBuilder(
  request: APIRequestContext,
): Promise<void> {
  await resetStack();

  // 1. Seed the org + owner.
  await request.post(`${YAAOS_URL}/api/testing/seed/bootstrap_owner`, {
    data: {
      email: "owner@readonly.test",
      github_id: OWNER_GITHUB_ID,
      org_slug: ORG_SLUG,
      display_name: "Owner",
      provider: "test",
    },
  });

  // 2. Seed a builder member on the same org.
  await request.post(`${YAAOS_URL}/api/testing/seed/member_for_org`, {
    data: {
      org_slug: ORG_SLUG,
      email: "builder@readonly.test",
      github_id: BUILDER_GITHUB_ID,
      role: "builder",
      display_name: "Builder",
      provider: "test",
    },
  });
}

async function loginAsBuilder(page: Page): Promise<void> {
  await page.request.post(`${YAAOS_URL}/api/testing/oauth_test/stage_profile`, {
    data: {
      external_subject: BUILDER_GITHUB_ID,
      primary_email: "builder@readonly.test",
      email_verified: true,
      display_name: "Builder",
    },
  });
  await page.goto(`${YAAOS_URL}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(new RegExp(`/org/${ORG_SLUG}/workspaces$`));
}

async function seedActiveAgent(
  request: APIRequestContext,
): Promise<{ id: string; instance_id: string }> {
  const r = await request.post(`${YAAOS_URL}/api/testing/seed/workspace_agent`, {
    data: { org_slug: ORG_SLUG, lifecycle: "active" },
  });
  if (!r.ok()) {
    throw new Error(`seed agent → ${r.status()}: ${await r.text()}`);
  }
  return (await r.json()) as { id: string; instance_id: string };
}

test.describe("builder readonly view", () => {
  test("builder sees agent cards but no checkboxes or bulk-action buttons", async ({
    page,
    request,
  }) => {
    await setupOrgWithBuilder(request);

    // Seed agents while logged out (using the API directly).
    const agent = await seedActiveAgent(request);

    await loginAsBuilder(page);
    await expect(page.getByTestId("workspaces-page")).toBeVisible({ timeout: 10_000 });

    // Card is visible.
    await expect(
      page.getByTestId(`workspaces-agent-card-${agent.instance_id}`),
    ).toBeVisible({ timeout: 15_000 });

    // Active section exists.
    await expect(page.getByTestId("workspaces-section-active")).toBeVisible();

    // No checkboxes, no select-all, no bulk button.
    await expect(
      page.getByTestId(`workspaces-agent-card-${agent.instance_id}-select`),
    ).not.toBeVisible();
    await expect(
      page.getByTestId("workspaces-section-active-select-all"),
    ).not.toBeVisible();
    await expect(
      page.getByTestId("workspaces-section-active-shutdown"),
    ).not.toBeVisible();
  });
});
