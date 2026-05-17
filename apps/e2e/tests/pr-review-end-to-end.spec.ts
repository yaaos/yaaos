/**
 * The headline journey: a PR arrives → yaaos reviews it → user sees findings.
 *
 * Boundary: fake-github dispatches a `pull_request.opened` webhook; yaaos
 * creates a ticket, runs one review (the parent reviewer dispatches yaaos-*
 * subagents internally; the stub coding-agent short-circuits the CLI),
 * posts one Review back to fake-github, and the user sees the results in
 * the UI. No interaction with the test runner once dispatched.
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

test("PR open → reviewer posts; ticket detail renders findings", async ({ page }) => {
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({
      repo: "acme/api",
      number: 42,
      title: "Add /metrics endpoint",
      body: "Adds a Prometheus metrics endpoint.",
    }),
  });

  // Ticket appears in the list within a few SSE/polling ticks.
  await page.goto("/tickets");
  await expect(page.getByTestId("tickets-list")).toContainText("Add /metrics endpoint", {
    timeout: 20_000,
  });

  // Open the ticket. The review reaches `posted`.
  await page.getByText("Add /metrics endpoint").click();
  await expect(page.getByTestId("ticket-detail")).toBeVisible();
  await expect
    .poll(() => page.locator('[data-testid^="agent-card-"][data-state="posted"]').count(), {
      timeout: 30_000,
    })
    .toBe(1);

  // SummaryStrip is populated (any value beats the loading state).
  await expect(page.getByTestId("summary-strip")).toBeVisible();

  // fake-github recorded the post.
  const comments = await postedComments();
  expect(comments.length).toBeGreaterThanOrEqual(1);
});
