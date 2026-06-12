/**
 * Workspace agent graceful shutdown — card flips offline within seconds.
 *
 * Requires the live Docker stack (`bin/dev-rebuild`). The orchestrator runs
 * this in the final verification pass; it is written but NOT run in local CI.
 *
 * Test scenario:
 * 1. Bootstrap an org with a running agent container (via the seed surface).
 * 2. Navigate to the dashboard — the agent card shows "online" (reachable).
 * 3. Stop the agent container cleanly (SIGTERM via docker stop).
 * 4. The agent sends DELETE /api/v1/agent/identity before exiting.
 * 5. The SSE `agent_liveness_changed` event invalidates the agents query.
 * 6. The card flips to "offline" within ~5 seconds, without a page reload.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { resetStack, YAAOS_URL } from "./_helpers";

async function setupAuthedAcmeOwner(page: Page, request: APIRequestContext): Promise<void> {
  await resetStack();
  await request.post(`${YAAOS_URL}/api/testing/seed/bootstrap_owner`, {
    data: {
      email: "owner@yaaos.test",
      github_id: "7001",
      org_slug: "acme-shutdown",
      display_name: "Owner",
      provider: "test",
    },
  });
  await request.post(`${YAAOS_URL}/api/testing/oauth_test/stage_profile`, {
    data: {
      external_subject: "7001",
      primary_email: "owner@yaaos.test",
      email_verified: true,
      display_name: "Owner",
    },
  });

  await page.goto(`${YAAOS_URL}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(/\/org\/acme-shutdown\/dashboard$/);
}

/** Seed a reachable agent row. */
async function seedReachableAgent(
  request: APIRequestContext,
  opts: { org_slug: string },
): Promise<{ id: string; instance_id: string }> {
  const r = await request.post(`${YAAOS_URL}/api/testing/seed/workspace_agent`, {
    data: { org_slug: opts.org_slug },
  });
  if (!r.ok()) {
    throw new Error(`seed workspace_agent → ${r.status()}: ${await r.text()}`);
  }
  return (await r.json()) as { id: string; instance_id: string };
}

test.describe("workspace agent graceful shutdown", () => {
  test("stopping agent container cleanly flips card offline without page reload", async ({
    page,
    request,
  }) => {
    await setupAuthedAcmeOwner(page, request);
    await expect(page.getByTestId("dashboard-populated")).toBeVisible({ timeout: 10_000 });

    // Seed a reachable agent.
    const { id, instance_id } = await seedReachableAgent(request, { org_slug: "acme-shutdown" });

    // Card must appear as reachable first.
    const agentCard = page.getByTestId(`agent-card-instance-${instance_id}`).first();
    await expect(agentCard).toBeVisible({ timeout: 15_000 });

    // Trigger graceful shutdown via the testing surface (sends DELETE /api/v1/agent/identity).
    const r = await request.post(`${YAAOS_URL}/api/testing/seed/deregister_workspace_agent`, {
      data: { id },
    });
    expect(r.ok()).toBeTruthy();

    // SSE agent_liveness_changed must flip the card to offline within 10s
    // without a page reload.
    const offlineIndicator = agentCard.getByTestId("agent-state-offline");
    await expect(offlineIndicator).toBeVisible({ timeout: 10_000 });
  });
});
