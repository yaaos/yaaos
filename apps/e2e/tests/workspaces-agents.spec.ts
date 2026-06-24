/**
 * Workspaces page — live agent cards and section rendering.
 *
 * Boundary: the backend seed surface creates agent rows; the Workspaces page
 * groups them into sections and renders cards live via SSE invalidation.
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

  await page.goto(`${YAAOS_URL}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(/\/org\/acme\/workspaces$/);
}

/** Seed a workspace agent row via the testing surface. */
async function seedAgent(
  request: APIRequestContext,
  opts: { org_slug: string; lifecycle?: string },
): Promise<{ id: string; instance_id: string }> {
  const r = await request.post(`${YAAOS_URL}/api/testing/seed/workspace_agent`, {
    data: { org_slug: opts.org_slug, lifecycle: opts.lifecycle },
  });
  if (!r.ok()) {
    throw new Error(`seed workspace_agent → ${r.status()}: ${await r.text()}`);
  }
  return (await r.json()) as { id: string; instance_id: string };
}

test.describe("workspaces page agent cards", () => {
  test("agent card appears after seeding; SSE invalidation works without reload", async ({
    page,
    request,
  }) => {
    await setupAuthedAcmeOwner(page, request);
    await expect(page.getByTestId("workspaces-page")).toBeVisible({ timeout: 10_000 });

    // Seed a fresh agent via the testing surface.
    const { instance_id } = await seedAgent(request, { org_slug: "acme" });

    // The SSE `agent_changed` event should invalidate the agents query
    // and cause the card to appear without a manual reload.
    const agentCard = page.getByTestId(`workspaces-agent-card-${instance_id}`).first();
    await expect(agentCard).toBeVisible({ timeout: 15_000 });
  });

  test("seeded agent appears in the Unconfigured section", async ({ page, request }) => {
    await setupAuthedAcmeOwner(page, request);
    await expect(page.getByTestId("workspaces-page")).toBeVisible({ timeout: 10_000 });

    // Default seed creates agents with lifecycle="unconfigured".
    const { instance_id } = await seedAgent(request, { org_slug: "acme" });

    const agentCard = page.getByTestId(`workspaces-agent-card-${instance_id}`).first();
    await expect(agentCard).toBeVisible({ timeout: 15_000 });

    // The card should be inside the Unconfigured section.
    const unconfiguredSection = page.getByTestId("workspaces-section-unconfigured");
    await expect(unconfiguredSection).toBeVisible();
    await expect(unconfiguredSection.getByTestId(`workspaces-agent-card-${instance_id}`)).toBeVisible();
  });

  test("Active section renders when an active agent exists", async ({ page, request }) => {
    await setupAuthedAcmeOwner(page, request);
    await expect(page.getByTestId("workspaces-page")).toBeVisible({ timeout: 10_000 });

    const { instance_id } = await seedAgent(request, { org_slug: "acme", lifecycle: "active" });

    const agentCard = page.getByTestId(`workspaces-agent-card-${instance_id}`).first();
    await expect(agentCard).toBeVisible({ timeout: 15_000 });

    // Active section must render; no Unconfigured section since only one agent.
    await expect(page.getByTestId("workspaces-section-active")).toBeVisible();
  });

  test("card status pair shows correct text for the agent's state and lifecycle", async ({
    page,
    request,
  }) => {
    await setupAuthedAcmeOwner(page, request);
    await expect(page.getByTestId("workspaces-page")).toBeVisible({ timeout: 10_000 });

    const { instance_id } = await seedAgent(request, { org_slug: "acme" });
    await expect(page.getByTestId(`workspaces-agent-card-${instance_id}`)).toBeVisible({ timeout: 15_000 });

    // Seeded agents start reachable + unconfigured.
    const status = page.getByTestId(`workspaces-agent-card-${instance_id}-status`);
    await expect(status).toContainText("Online");
    await expect(status).toContainText("Unconfigured");
  });
});
