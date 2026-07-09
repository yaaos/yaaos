/**
 * Ticket page Runs + Artifacts tabs.
 *
 * Seeds the same kind of paused run as `pipeline-run-overview.spec.ts` (see
 * that file's boundary note) — this spec asserts the Runs tab's stage-row
 * rendering (boundary outcome included) and the Artifacts tab's version
 * dropdown + rendered markdown body.
 *
 * Requires the live Docker stack (`bin/dev-rebuild`).
 */

import { expect, test } from "@playwright/test";
import { loginAsOwner, seedPausedRun, YAAOS_URL } from "./_helpers";

const ORG_SLUG = "acme-pipeline-tabs";

test("Runs tab shows a stage row with its boundary outcome", async ({ page, request }) => {
  await loginAsOwner(page, request, ORG_SLUG);
  const seeded = await seedPausedRun({ orgSlug: ORG_SLUG, ticketTitle: "Runs tab stage row" });

  await page.goto(`${YAAOS_URL}/org/${ORG_SLUG}/tickets/${seeded.ticket_id}`);
  await page.getByTestId("ticket-tab-runs").click();
  await expect(page.getByTestId("ticket-runs")).toBeVisible();

  const runCard = page.getByTestId(`run-card-${seeded.run_id}`);
  await expect(runCard).toBeVisible();

  const stageRow = page.getByTestId("stage-row-write-spec");
  await expect(stageRow).toBeVisible();
  await expect(stageRow).toContainText("completed");
  await expect(stageRow).toContainText("paused");
  await expect(stageRow).toContainText("high");
});

test("Artifacts tab renders the stored artifact with a version dropdown", async ({
  page,
  request,
}) => {
  await loginAsOwner(page, request, ORG_SLUG);
  const seeded = await seedPausedRun({ orgSlug: ORG_SLUG, ticketTitle: "Artifacts tab lineage" });

  await page.goto(`${YAAOS_URL}/org/${ORG_SLUG}/tickets/${seeded.ticket_id}`);
  await page.getByTestId("ticket-tab-artifacts").click();
  await expect(page.getByTestId("ticket-artifacts")).toBeVisible();

  const lineage = page.getByTestId("artifact-lineage-write-spec");
  await expect(lineage).toBeVisible();
  await expect(lineage).toContainText("write-spec");
  // The version Select's trigger renders the selected option's label
  // ("v1 · <pipeline> · <ago>") — a single seeded version.
  await expect(lineage).toContainText("v1");
  await expect(lineage.getByText("Seeded artifact body for e2e.")).toBeVisible();
});
