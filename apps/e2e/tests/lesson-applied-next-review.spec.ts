/**
 * Lessons are passed into the agent invocation on subsequent reviews.
 *
 * Boundary: pre-seed a lesson for the repo, dispatch a PR, look at the
 * audit log's `review_job.prompt_sent` entries — they should report a
 * non-zero `lessons_count` (and the lesson's UUID in `lessons_applied`).
 */

import { expect, test } from "@playwright/test";
import {
  dispatchWebhook,
  prPayload,
  resetStack,
  seedCredentialsAndInstall,
  seedLesson,
  YAAOS_URL,
} from "./_helpers";

test.beforeEach(async () => {
  await resetStack();
  await seedCredentialsAndInstall();
});

test("a pre-existing lesson shows up in the prompt_sent audit payload", async ({ page }) => {
  await seedLesson({
    repo_external_id: "acme/api",
    title: "Cite the CWE family",
    body: "When flagging an input-validation issue, name the CWE family.",
  });

  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({ repo: "acme/api", number: 31, title: "Add user-input validation" }),
  });

  await page.goto("/tickets");
  await page.getByText("Add user-input validation").click();
  await expect
    .poll(() => page.locator('[data-testid^="agent-card-"][data-state="posted"]').count(), {
      timeout: 30_000,
    })
    .toBe(1);

  // Cross-check via the audit-log API rather than the UI (the audit
  // payload is JSON; the UI renders a summary). Pull the ticket id from
  // the current URL.
  const url = page.url();
  const m = url.match(/tickets\/([0-9a-f-]+)/);
  expect(m).not.toBeNull();
  const ticketId = m![1];

  const audit = (await (await fetch(`${YAAOS_URL}/api/tickets/${ticketId}/audit`)).json()) as Array<{
    kind: string;
    payload: { lessons_count?: number };
  }>;
  const promptSent = audit.filter((e) => e.kind === "review_job.prompt_sent");
  expect(promptSent.length).toBeGreaterThanOrEqual(1);
  expect(promptSent.every((e) => (e.payload.lessons_count ?? 0) >= 1)).toBe(true);
});
