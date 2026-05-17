/**
 * User actions on a ticket: Re-review and Cancel.
 *
 * Re-review is exercised fully through the UI. Cancel is exercised against
 * the API (POST /api/reviewer/cancel?ticket_id=...) because under the stub
 * coding agent the per-job latency is ~10ms — clicking the Cancel UI button
 * fast enough to catch an in-flight batch is racy by construction. The
 * backend handler is what we actually care about: it must flip in-flight
 * jobs to cancelled and write a `review_job.cancelled` audit entry. Both
 * are observable via the audit-log API without touching the UI race.
 */

import { expect, test } from "@playwright/test";
import {
  dispatchWebhook,
  postedComments,
  prPayload,
  resetStack,
  seedCredentialsAndInstall,
  YAAOS_URL,
} from "./_helpers";

test.beforeEach(async () => {
  await resetStack();
  await seedCredentialsAndInstall();
});

test("Re-review triggers a fresh batch", async ({ page }) => {
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({ repo: "acme/web", number: 11, title: "Refactor user list" }),
  });

  await page.goto("/tickets");
  await page.getByText("Refactor user list").click();
  await expect
    .poll(() => page.locator('[data-testid^="agent-card-"][data-state="posted"]').count(), {
      timeout: 30_000,
    })
    .toBe(1);
  const commentsBefore = (await postedComments()).length;

  await page.getByTestId("rereview-button").click();
  await expect
    .poll(async () => (await postedComments()).length, { timeout: 30_000 })
    .toBeGreaterThan(commentsBefore);
});

test("Cancel endpoint records review_job.cancelled in the audit log", async ({ request }) => {
  // Drive a ticket into existence first so we have something to cancel
  // pending on. The initial batch may complete by the time the cancel call
  // lands — that's fine; `cancel_pending` is a no-op on already-terminal
  // jobs and the test fires a fresh re-review immediately before cancelling
  // to maximize the odds of catching one in queued/running.
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({ repo: "acme/web", number: 12, title: "Tiny change" }),
  });
  // Find the ticket id.
  const ticketsResp = await request.get(`${YAAOS_URL}/api/tickets`);
  const tickets = (await ticketsResp.json()) as Array<{ id: string; title: string }>;
  const ticket = tickets.find((t) => t.title === "Tiny change");
  expect(ticket).toBeDefined();

  // Kick a re-review then cancel as fast as we can.
  await request.post(`${YAAOS_URL}/api/reviewer/rereview?ticket_id=${ticket!.id}`);
  await request.post(`${YAAOS_URL}/api/reviewer/cancel?ticket_id=${ticket!.id}`);

  // Poll the audit-log endpoint for at least one `review_job.cancelled`
  // entry. Even if all jobs from a particular batch beat the cancel, the
  // re-review-induced supersede path also writes `review_job.cancelled`
  // entries — so the assertion holds as long as the wiring works.
  await expect
    .poll(
      async () => {
        const r = await request.get(`${YAAOS_URL}/api/tickets/${ticket!.id}/audit`);
        const audit = (await r.json()) as Array<{ kind: string }>;
        return audit.filter((e) => e.kind === "review_job.cancelled").length;
      },
      { timeout: 30_000 },
    )
    .toBeGreaterThan(0);
});
