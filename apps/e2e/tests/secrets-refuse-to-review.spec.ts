/**
 * Pre-flight secrets check refuses to review and posts a single warning.
 *
 * Boundary: a PR whose diff contains an AWS-access-key-shaped string arrives;
 * the review job transitions to `skipped(secrets_detected)`; fake-github
 * received the refuse-to-review comment.
 */

import { expect, test } from "@playwright/test";
import {
  dispatchWebhook,
  postedComments,
  prPayload,
  resetStack,
  seedCredentialsAndInstall,
  seedPRDiff,
} from "./_helpers";

test.beforeEach(async () => {
  await resetStack();
  await seedCredentialsAndInstall();
});

test("PR with secret in diff is refused; review skips", async ({ page }) => {
  await seedPRDiff({
    repo: "acme/api",
    number: 99,
    diff: [
      "diff --git a/.env b/.env",
      "+++ b/.env",
      "+AWS_KEY=AKIAIOSFODNN7EXAMPLE",
    ].join("\n"),
    files: [{ filename: ".env", status: "modified", additions: 1, deletions: 0 }],
  });
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({
      repo: "acme/api",
      number: 99,
      title: "Add env file with credentials",
    }),
  });

  await page.goto("/tickets");
  await page.getByText("Add env file with credentials").click();

  // The review reaches `skipped` (must not reach `posted`).
  await expect
    .poll(() => page.locator('[data-testid^="agent-card-"][data-state="skipped"]').count(), {
      timeout: 30_000,
    })
    .toBe(1);
  await expect(
    page.locator('[data-testid^="agent-card-"][data-state="posted"]'),
  ).toHaveCount(0);

  // fake-github received the refuse-to-review notification as a top-level
  // PR comment (issue-comments endpoint).
  const comments = await postedComments();
  const bodies = comments.map((c) => String(c.body ?? ""));
  expect(bodies.some((b) => b.toLowerCase().includes("secret"))).toBe(true);
});
