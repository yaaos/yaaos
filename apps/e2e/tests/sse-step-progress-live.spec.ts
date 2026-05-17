/**
 * SSE pushes drive the running review-card state without page reloads.
 *
 * Boundary: open ticket detail with no prior reviews, dispatch a webhook,
 * watch the review card transition states without a manual refresh. We assert
 * the terminal state (`posted`) is reached — anything in between (queued,
 * running, posting_review) is incidental; what we're validating is that the
 * UI updates from SSE alone.
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

test("review card state transitions live without reload", async ({ page }) => {
  // Land on the tickets list first so the SSE subscriber mounts before any
  // events fly. Then dispatch.
  await page.goto("/tickets");
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({ repo: "acme/web", number: 55, title: "Live SSE check" }),
  });

  // The ticket appears via SSE invalidation.
  await expect(page.getByText("Live SSE check")).toBeVisible({ timeout: 20_000 });
  await page.getByText("Live SSE check").click();

  // Without refreshing the page, the review reaches the terminal `posted`
  // state — the transition is driven by SSE invalidations of
  // `["reviewer", "jobs", ticket_id]` triggered by `review_job_*` events.
  await expect
    .poll(() => page.locator('[data-testid^="agent-card-"][data-state="posted"]').count(), {
      timeout: 30_000,
    })
    .toBe(1);
});
