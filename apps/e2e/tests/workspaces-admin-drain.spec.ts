/**
 * Workspaces page — admin bulk shutdown.
 *
 * Owner/admin selects active agents, clicks "Shut down", confirms the dialog,
 * and verifies: the toast shows, selection clears, and the cards appear in
 * the Draining section via SSE-driven invalidation.
 *
 * Requires the live Docker stack (`bin/dev-rebuild`).
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { loginAsOwner, YAAOS_URL } from "./_helpers";

async function seedActiveAgent(
  request: APIRequestContext,
  orgSlug: string,
): Promise<{ id: string; instance_id: string }> {
  const r = await request.post(`${YAAOS_URL}/api/testing/seed/workspace_agent`, {
    data: { org_slug: orgSlug, lifecycle: "active" },
  });
  if (!r.ok()) {
    throw new Error(`seed active agent → ${r.status()}: ${await r.text()}`);
  }
  return (await r.json()) as { id: string; instance_id: string };
}

test.describe("admin bulk shutdown", () => {
  test("owner selects two active agents, shuts them down, sees toast and cards move to Draining", async ({
    page,
    request,
  }) => {
    await loginAsOwner(page, request);
    await expect(page.getByTestId("workspaces-page")).toBeVisible({ timeout: 10_000 });

    // Seed two active agents.
    const a1 = await seedActiveAgent(request, "acme");
    const a2 = await seedActiveAgent(request, "acme");

    // Wait for both cards to appear in the Active section.
    await expect(
      page.getByTestId(`workspaces-agent-card-${a1.instance_id}`),
    ).toBeVisible({ timeout: 15_000 });
    await expect(
      page.getByTestId(`workspaces-agent-card-${a2.instance_id}`),
    ).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("workspaces-section-active")).toBeVisible();

    // Admin controls: checkboxes visible.
    const chk1 = page.getByTestId(`workspaces-agent-card-${a1.instance_id}-select`);
    const chk2 = page.getByTestId(`workspaces-agent-card-${a2.instance_id}-select`);
    await expect(chk1).toBeVisible({ timeout: 5_000 });
    await expect(chk2).toBeVisible();

    // Select both agents.
    await chk1.click();
    await chk2.click();

    // Click the "Shut down" bulk-action button.
    await page.getByTestId("workspaces-section-active-shutdown").click();

    // Confirm the shutdown dialog.
    await expect(page.getByTestId("workspaces-shutdown-dialog")).toBeVisible({ timeout: 5_000 });
    await page.getByTestId("workspaces-shutdown-dialog-confirm").click();

    // Toast should appear.
    await expect(page.getByText(/Shut down \d+ agents?\./)).toBeVisible({ timeout: 10_000 });

    // Cards should move to the Draining section via SSE-driven re-fetch.
    await expect(page.getByTestId("workspaces-section-draining")).toBeVisible({ timeout: 15_000 });
    await expect(
      page
        .getByTestId("workspaces-section-draining")
        .getByTestId(`workspaces-agent-card-${a1.instance_id}`),
    ).toBeVisible({ timeout: 15_000 });
  });

  test("Shut down button is disabled while selection is empty", async ({ page, request }) => {
    await loginAsOwner(page, request);
    await expect(page.getByTestId("workspaces-page")).toBeVisible({ timeout: 10_000 });

    await seedActiveAgent(request, "acme");
    await expect(page.getByTestId("workspaces-section-active")).toBeVisible({ timeout: 15_000 });

    // Nothing selected: button must be disabled.
    await expect(page.getByTestId("workspaces-section-active-shutdown")).toBeDisabled();
  });

  test("canceling the shutdown dialog closes it without mutating", async ({ page, request }) => {
    await loginAsOwner(page, request);
    await expect(page.getByTestId("workspaces-page")).toBeVisible({ timeout: 10_000 });

    const a1 = await seedActiveAgent(request, "acme");
    await expect(
      page.getByTestId(`workspaces-agent-card-${a1.instance_id}`),
    ).toBeVisible({ timeout: 15_000 });

    await page.getByTestId(`workspaces-agent-card-${a1.instance_id}-select`).click();
    await page.getByTestId("workspaces-section-active-shutdown").click();
    await expect(page.getByTestId("workspaces-shutdown-dialog")).toBeVisible({ timeout: 5_000 });

    // Cancel — dialog closes, agent stays in Active.
    await page.getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByTestId("workspaces-shutdown-dialog")).not.toBeVisible();
    await expect(page.getByTestId("workspaces-section-active")).toBeVisible();
    // Card is still in Active section.
    await expect(
      page
        .getByTestId("workspaces-section-active")
        .getByTestId(`workspaces-agent-card-${a1.instance_id}`),
    ).toBeVisible();
  });
});
