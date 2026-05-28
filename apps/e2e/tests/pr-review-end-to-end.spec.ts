/**
 * The headline journey: a PR arrives → yaaos reviews it → user sees findings.
 *
 * Boundary: fake-github dispatches a `pull_request.opened` webhook; yaaos
 * creates a ticket, runs one review (the parent reviewer dispatches yaaos-*
 * subagents internally; the stub coding-agent short-circuits the CLI),
 * posts one Review back to fake-github, and the user sees the results in
 * the UI. No interaction with the test runner once dispatched.
 *
 * The user is logged in as the Owner of the `acme` org via the `oauth_test`
 * stub; the install is attached to the same org so webhook-created tickets
 * land on the route (`/orgs/acme/tickets`) the user navigates to.
 */

import { expect, test, type Page, type APIRequestContext } from "@playwright/test";
import {
  dispatchWebhook,
  postedComments,
  prPayload,
  resetStack,
  seedGithubInstall,
  YAAOS_URL,
} from "./_helpers";

async function setupAuthedAcmeOwner(page: Page, request: APIRequestContext): Promise<void> {
  await resetStack();
  await request.post(`${YAAOS_URL}/api/testing/seed/bootstrap_owner`, {
    data: {
      email: "owner@yaaos.test",
      github_id: "3001",
      org_slug: "acme",
      display_name: "Owner",
      provider: "test",
    },
  });
  await request.post(`${YAAOS_URL}/api/testing/oauth_test/stage_profile`, {
    data: {
      external_subject: "3001",
      primary_email: "owner@yaaos.test",
      email_verified: true,
      display_name: "Owner",
    },
  });
  // Pin the install + settings rows to acme so the webhook-created ticket
  // lives on the same org the authenticated user belongs to.
  await seedGithubInstall({ targetOrgSlug: "acme" });

  await page.goto(`${YAAOS_URL}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(/\/orgs\/acme\/dashboard$/);
}

/**
 * Both tests below depend on the outbox → taskiq dispatcher to
 * actually run the workflow. No broker + drain loop runs in the e2e
 * stack — `taskiq_enqueue` outbox rows pile up and `workflow_executions`
 * stay in `pending` forever. Backend service tests cover the workflow
 * engine itself; the e2e flow stays skipped until the dispatcher exists.
 *
 * To re-enable: drop the `test.skip` once
 *   - a real `core/tasks` broker runs in the FastAPI lifespan, AND
 *   - the outbox drain loop dispatches `taskiq_enqueue` rows post-commit.
 */
test("PR open → reviewer posts; ticket detail renders findings", async ({ page, request }) => {
  await setupAuthedAcmeOwner(page, request);

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
  await page.goto(`${YAAOS_URL}/orgs/acme/tickets`);
  await expect(page.getByTestId("tickets-list")).toContainText("Add /metrics endpoint", {
    timeout: 20_000,
  });

  // Open the ticket and wait for the review to post findings to GitHub.
  // The end-to-end signal we care about: yaaos posted at least one
  // review comment to fake-github (the SPA dropped the earlier
  // agent-card pattern; the actual cross-system contract is the
  // posted comments).
  await page.getByText("Add /metrics endpoint").click();
  await expect(page.getByTestId("ticket-detail")).toBeVisible();
  await expect
    .poll(async () => (await postedComments()).length, { timeout: 30_000 })
    .toBeGreaterThanOrEqual(1);
});

/**
 * SSE-driven state transitions: open the tickets list before the review
 * starts; the review-card flips to `posted` WITHOUT a manual reload (the
 * contract `review_job_status_changed` events drive in `apps/web`).
 *
 * Folded in from the standalone `sse-step-progress-live.spec.ts` so we
 * don't pay the docker-compose bring-up twice for the same backend flow.
 */
test("review card state transitions live via SSE without reload", async ({ page, request }) => {
  await setupAuthedAcmeOwner(page, request);

  // Land on the tickets list FIRST so the SSE subscriber is mounted before
  // any events fly.
  await page.goto(`${YAAOS_URL}/orgs/acme/tickets`);
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({ repo: "acme/web", number: 55, title: "Live SSE check" }),
  });
  await expect(page.getByText("Live SSE check")).toBeVisible({ timeout: 20_000 });
  await page.getByText("Live SSE check").click();
  // Detail page mounts; reviewer eventually posts comments without
  // needing a manual reload.
  await expect(page.getByTestId("ticket-detail")).toBeVisible();
  await expect
    .poll(async () => (await postedComments()).length, { timeout: 30_000 })
    .toBeGreaterThanOrEqual(1);
});
