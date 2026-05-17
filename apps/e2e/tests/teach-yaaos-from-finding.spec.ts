/**
 * The memory loop entry-point: a posted finding → Teach yaaos → new lesson.
 *
 * Boundary: clicking "Teach yaaos…" on a finding opens the modal pre-filled
 * with the finding's body. After save, the new lesson appears on the Memory
 * page under the finding's repo.
 */

import { expect, test } from "@playwright/test";
import {
  dispatchWebhook,
  prPayload,
  resetStack,
  seedCredentialsAndInstall,
} from "./_helpers";

test.beforeEach(async () => {
  await resetStack();
  await seedCredentialsAndInstall();
});

test("Teach yaaos from a finding creates a lesson on the finding's repo", async ({ page }) => {
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({ repo: "acme/web", number: 21, title: "Tighten user list api" }),
  });

  await page.goto("/tickets");
  await page.getByText("Tighten user list api").click();
  await expect
    .poll(() => page.locator('[data-testid^="agent-card-"][data-state="posted"]').count(), {
      timeout: 30_000,
    })
    .toBe(1);

  // Expand the first finding and click Teach.
  await page.getByTestId("findings-list").locator("li").first().click();
  await page.getByTestId("teach-yaaos").first().click();

  // Fill title (body pre-fills from the finding) and save.
  const lessonTitle = `cite the CWE family ${Date.now()}`;
  await page.getByTestId("teach-title").fill(lessonTitle);
  await page.getByTestId("teach-save").click();

  // Memory page shows the new lesson for the correct repo.
  await page.goto("/memory");
  await expect(page.getByTestId("lessons-list")).toContainText(lessonTitle, { timeout: 10_000 });
});
