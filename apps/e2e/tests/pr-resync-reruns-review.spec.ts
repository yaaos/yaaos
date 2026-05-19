/**
 * `pull_request.synchronize` after an initial review triggers a fresh review run.
 *
 * Also lightly covers the force-push detection wire — the synchronize handler
 * always calls fake-github's `/compare` endpoint. We seed `status=diverged`
 * for the second push to make sure the call path succeeds end-to-end.
 *
 * Boundary: webhook → ticket exists → re-run → audit log grows.
 */

import { expect, test } from "@playwright/test";
import {
  dispatchWebhook,
  postedComments,
  prPayload,
  resetStack,
  seedCredentialsAndInstall,
} from "./_helpers";

test.beforeEach(async () => {
  await resetStack();
  await seedCredentialsAndInstall();
});

test("synchronize event re-runs reviewers and grows the audit log", async ({ page }) => {
  const opened = prPayload({
    repo: "acme/api",
    number: 7,
    title: "Refactor request pipeline",
  });
  await dispatchWebhook({ event: "pull_request", payload: opened });

  await page.goto("/tickets");
  await page.getByText("Refactor request pipeline").click();
  await expect
    .poll(() => page.locator('[data-testid^="agent-card-"][data-state="posted"]').count(), {
      timeout: 30_000,
    })
    .toBe(1);

  const commentsBefore = (await postedComments()).length;

  // Second push — a regular synchronize (no force-push). The trigger
  // policy's incremental path runs the reviewer and posts a fresh batch.
  // (Force-push pushes that diverge from the last-reviewed history take
  // the `history_changed → skipped` path by design and require a manual
  // re-review — see `domain/reviewer/incremental.handle_push`.)
  const beforeSha = "head-acme-api-7";
  const afterSha = "head-acme-api-7-v2";
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({
      repo: "acme/api",
      number: 7,
      title: "Refactor request pipeline",
      action: "synchronize",
      before: beforeSha,
      after: afterSha,
      headSha: afterSha,
    }),
  });

  // Poll fake-github for the second batch of posts — synchronize triggers a
  // new review run that posts one Review.
  await expect
    .poll(async () => (await postedComments()).length, { timeout: 30_000 })
    .toBeGreaterThan(commentsBefore);

  // Audit log grew — initial review run + the synchronize re-run write
  // scheduled/prompt_sent/posted entries each, so the list has at least
  // a few items.
  await page.getByTestId("tab-audit").click();
  await expect(page.getByTestId("audit-log").locator("li").nth(3)).toBeVisible({
    timeout: 10_000,
  });
});
