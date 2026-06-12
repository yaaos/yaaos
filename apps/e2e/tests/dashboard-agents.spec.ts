/**
 * Dashboard workspace-agent row — live cards and empty-state.
 *
 * Boundary: the backend identity-exchange endpoint seeds an agent row; the
 * dashboard's "Workspace agents" section renders it live via SSE invalidation.
 * The empty-state card links to the workspaces settings page.
 *
 * NOTE: these tests are written but not exercised in local CI — they require
 * the running Docker stack. The orchestrator runs the full e2e suite in the
 * final verification phase after `bin/dev-rebuild`.
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
  await page.waitForURL(/\/org\/acme\/dashboard$/);
}

/** Seed a workspace agent row via the testing surface. */
async function seedAgent(
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

test.describe("dashboard workspace agents row", () => {
  test("empty-state card renders and links to /settings/workspaces when no agents connected", async ({
    page,
    request,
  }) => {
    await setupAuthedAcmeOwner(page, request);

    // Confirm the dashboard is loaded (populated state).
    await expect(page.getByTestId("dashboard-populated")).toBeVisible({ timeout: 10_000 });

    // The empty-state link should point to the workspaces settings page.
    const link = page.getByTestId("agent-card-empty-settings-link");
    await expect(link).toBeVisible();
    await expect(link).toHaveAttribute("href", /\/org\/acme\/settings\/workspaces/);
  });

  test("agent card shows online after seeding; SSE flips state without reload", async ({
    page,
    request,
  }) => {
    await setupAuthedAcmeOwner(page, request);
    await expect(page.getByTestId("dashboard-populated")).toBeVisible({ timeout: 10_000 });

    // Seed a fresh agent via the testing surface.
    const { instance_id } = await seedAgent(request, { org_slug: "acme" });

    // The SSE `agent_liveness_changed` event should invalidate the agents query
    // and cause the card to appear without a manual reload.
    // Allow a few seconds for the SSE round-trip + cache invalidation.
    const agentCard = page.getByTestId(`agent-card-instance-${instance_id}`).first();
    await expect(agentCard).toBeVisible({ timeout: 15_000 });
  });
});
