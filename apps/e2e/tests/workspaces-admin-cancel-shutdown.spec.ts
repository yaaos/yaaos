/**
 * Workspaces page — admin bulk cancel-shutdown.
 *
 * Owner/admin selects draining agents, clicks "Cancel shutdown", confirms the
 * dialog, and verifies the toast shows and the cards move back to Active.
 *
 * Requires the live Docker stack (`bin/dev-rebuild`).
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { loginAsOwner, YAAOS_URL } from "./_helpers";

async function seedDrainingAgent(
  request: APIRequestContext,
  orgSlug: string,
): Promise<{ id: string; instance_id: string }> {
  const r = await request.post(`${YAAOS_URL}/api/testing/seed/workspace_agent`, {
    data: { org_slug: orgSlug, lifecycle: "draining" },
  });
  if (!r.ok()) {
    throw new Error(`seed draining agent → ${r.status()}: ${await r.text()}`);
  }
  return (await r.json()) as { id: string; instance_id: string };
}

test.describe("admin bulk cancel-shutdown", () => {
  test("owner selects draining agent, cancels shutdown, sees toast and card moves to Active", async ({
    page,
    request,
  }) => {
    await loginAsOwner(page, request);
    await expect(page.getByTestId("workspaces-page")).toBeVisible({ timeout: 10_000 });

    const d1 = await seedDrainingAgent(request, "acme");

    // Wait for the Draining section and the card.
    await expect(
      page.getByTestId(`workspaces-agent-card-${d1.instance_id}`),
    ).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("workspaces-section-draining")).toBeVisible();

    // Select the agent.
    const chk = page.getByTestId(`workspaces-agent-card-${d1.instance_id}-select`);
    await expect(chk).toBeVisible({ timeout: 5_000 });
    await chk.click();

    // Click "Cancel shutdown".
    await page.getByTestId("workspaces-section-draining-cancel-shutdown").click();

    // Confirm via the dialog.
    await expect(
      page.getByTestId("workspaces-cancel-shutdown-dialog"),
    ).toBeVisible({ timeout: 5_000 });
    await page.getByTestId("workspaces-cancel-shutdown-dialog-confirm").click();

    // Toast should appear.
    await expect(page.getByText(/Canceled shutdown for \d+ agents?\./)).toBeVisible({
      timeout: 10_000,
    });

    // Card should appear in the Active section.
    await expect(page.getByTestId("workspaces-section-active")).toBeVisible({ timeout: 15_000 });
    await expect(
      page
        .getByTestId("workspaces-section-active")
        .getByTestId(`workspaces-agent-card-${d1.instance_id}`),
    ).toBeVisible({ timeout: 15_000 });
  });

  test("Cancel shutdown button is disabled while draining selection is empty", async ({
    page,
    request,
  }) => {
    await loginAsOwner(page, request);
    await expect(page.getByTestId("workspaces-page")).toBeVisible({ timeout: 10_000 });

    await seedDrainingAgent(request, "acme");
    await expect(
      page.getByTestId("workspaces-section-draining"),
    ).toBeVisible({ timeout: 15_000 });

    await expect(
      page.getByTestId("workspaces-section-draining-cancel-shutdown"),
    ).toBeDisabled();
  });
});
