/**
 * Ticket page live-activity tail — SSE streaming of agent output frames
 * while a stage is running.
 *
 * Seeds a `running` pipeline run (no real coding-agent involved), opens the
 * ticket page, injects synthetic activity frames via the test publish shim,
 * and asserts that the live-tail pane updates in real time — zero page reloads.
 *
 * Requires the live Docker stack (`bin/dev-rebuild`).
 */

import { expect, test } from "@playwright/test";
import {
  YAAOS_URL,
  loginAsOwner,
  publishWorkspaceActivity,
  seedRunningRun,
} from "./_helpers";

const ORG_SLUG = "acme-live-activity";

test("live activity rows appear in stage-activity-live without a page reload", async ({
  page,
  request,
}) => {
  await loginAsOwner(page, request, ORG_SLUG);

  const seeded = await seedRunningRun({
    orgSlug: ORG_SLUG,
    ticketTitle: "Live activity test run",
    stageName: "write-code",
  });

  // Navigate to the ticket page. Overview tab loads first and shows in_flight.
  await page.goto(`${YAAOS_URL}/org/${ORG_SLUG}/tickets/${seeded.ticket_id}`);
  await expect(page.getByTestId("attention-block")).toBeVisible();
  await expect(page.getByTestId("attention-block")).toHaveAttribute("data-state", "in_flight");

  // Switch to the Runs tab and expand the Activity accordion for the running stage.
  await page.getByTestId("ticket-tab-runs").click();
  await expect(page.getByTestId("ticket-runs")).toBeVisible();

  // The stage row should appear with status "running".
  await expect(page.getByTestId("stage-row-write-code")).toBeVisible();

  // Click the Activity toggle button to open the live-tail pane.
  await page.getByTestId("stage-activity-toggle-write-code").click();

  // The live pane should appear with the placeholder copy.
  const livePaneLocator = page.getByTestId("stage-activity-live");
  await expect(livePaneLocator).toBeVisible();
  await expect(livePaneLocator).toContainText("Streaming live");

  // Wait until the EventSource is fully connected (data-connected="true") before
  // publishing — the SSE stream's Redis subscription is confirmed at onopen,
  // so any publish after this point is guaranteed to be received.
  await expect(livePaneLocator).toHaveAttribute("data-connected", "true", { timeout: 5_000 });

  // No rows yet.
  await expect(livePaneLocator.getByTestId("activity-event-row")).toHaveCount(0);

  // Publish the first synthetic activity frame via the test shim.
  const now = new Date().toISOString();
  await publishWorkspaceActivity({
    orgId: seeded.org_id,
    runId: seeded.run_id,
    payload: { kind: "assistant_message", ts: now, message: "reading the diff", detail: null },
  });

  // The row should appear live — zero page reloads.
  await expect(livePaneLocator.getByTestId("activity-event-row")).toHaveCount(1, {
    timeout: 10_000,
  });
  await expect(livePaneLocator).toContainText("reading the diff");

  // Publish a second frame.
  await publishWorkspaceActivity({
    orgId: seeded.org_id,
    runId: seeded.run_id,
    payload: {
      kind: "tool_call_started",
      ts: new Date().toISOString(),
      message: "Read",
      detail: { tool: "Read" },
    },
  });

  await expect(livePaneLocator.getByTestId("activity-event-row")).toHaveCount(2, {
    timeout: 10_000,
  });
});

test("overview-live-ticker shows the most recent activity message", async ({ page, request }) => {
  await loginAsOwner(page, request, ORG_SLUG);

  const seeded = await seedRunningRun({
    orgSlug: ORG_SLUG,
    ticketTitle: "Live ticker test run",
    stageName: "analyze",
  });

  await page.goto(`${YAAOS_URL}/org/${ORG_SLUG}/tickets/${seeded.ticket_id}`);
  await expect(page.getByTestId("attention-block")).toBeVisible();

  // Wait until the EventSource opened by InFlightCard is fully connected before
  // publishing — data-connected="true" means onopen has fired and the backend
  // Redis subscription is confirmed.
  await expect(page.getByTestId("attention-block")).toHaveAttribute("data-connected", "true", {
    timeout: 5_000,
  });

  // No ticker before any frames arrive.
  await expect(page.getByTestId("overview-live-ticker")).toHaveCount(0);

  // Publish a frame.
  const now = new Date().toISOString();
  await publishWorkspaceActivity({
    orgId: seeded.org_id,
    runId: seeded.run_id,
    payload: { kind: "assistant_message", ts: now, message: "analyzing code paths", detail: null },
  });

  // Ticker appears with the message — live, no reload.
  const ticker = page.getByTestId("overview-live-ticker");
  await expect(ticker).toBeVisible({ timeout: 10_000 });
  await expect(ticker).toContainText("analyzing code paths");

  // Clicking the ticker switches to the Runs tab.
  await ticker.click();
  await expect(page.getByTestId("ticket-runs")).toBeVisible();
});
