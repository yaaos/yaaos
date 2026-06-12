/**
 * The headline journey: a PR arrives → yaaos reviews it via the real Go
 * agent → findings post to fake-github.
 *
 * Boundary: fake-github dispatches a `pull_request.opened` webhook; yaaos
 * creates a ticket; the workflow engine dispatches `ProvisionWorkspace` →
 * `InvokeClaudeCode` → `CleanupWorkspace` AgentCommands to the real Go agent
 * running in the test stack; the agent fork/execs `fake-claude` (the bash
 * replay script bind-mounted at `/usr/local/bin/claude`); fake-claude emits
 * the `happy.jsonl` scenario stream-json; the backend parses the stdout,
 * validates findings, and posts them to fake-github via `post_finding`.
 *
 * This spec asserts the **cross-plane wire**:
 *   - An `InvokeClaudeCode` AgentCommand was dispatched AND consumed by the
 *     real Go agent (the stub workspace provider must NOT be active —
 *     `YAAOS_CODING_AGENT_STUB` must be unset/0 in the test stack).
 *   - The finding posted to fake-github contains content only producible by
 *     the `happy.jsonl` fake-claude scenario (`rule_violated: "no-unused-vars"`),
 *     which proves the stub's canned `stub/sample-suggestion` output was NOT
 *     used.
 *
 * Requires:
 *   - Real Go agent + mock-aws in the Docker test stack.
 *   - `fake-claude` bind-mounted at `/usr/local/bin/claude`.
 *   - `YAAOS_CODING_AGENT_STUB` unset or `0` in web and worker containers.
 *   - fake-github able to serve git HTTP (for `ProvisionWorkspace` clone step).
 */

import { expect, test, type Page, type APIRequestContext } from "@playwright/test";
import {
  dispatchWebhook,
  gitHeadSha,
  postedComments,
  prPayload,
  resetStack,
  seedGithubInstall,
  seedRepoSkill,
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
  // Seed skill_name for every scenario repo so build_review_invocation can
  // resolve the skill handle. "code-review" matches the SKILL.md in these repos.
  for (const repo of ["acme/review-happy", "acme/review-nonconforming", "acme/review-agentfail"]) {
    await seedRepoSkill({ orgSlug: "acme", repoExternalId: repo, skillName: "code-review" });
  }

  await page.goto(`${YAAOS_URL}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(/\/org\/acme\/dashboard$/);
}

/**
 * Happy path: PR open → real agent claims InvokeClaudeCode → fake-claude
 * (happy.jsonl) → canonical finding posted to fake-github.
 *
 * The spec seeds the PR under `acme/review-happy` so that the bash
 * `fake-claude` script matches the repo slug and cats `happy.jsonl`.
 * The posted comment body must contain `no-unused-vars` — the specific
 * `rule_violated` value from happy.jsonl — proving the real agent path
 * (not the in-process stub) produced the finding.
 */
test("PR open → real agent claims InvokeClaudeCode → findings post to fake-github", async ({
  page,
  request,
}) => {
  await setupAuthedAcmeOwner(page, request);

  // Use the real HEAD SHA from the fake-github bare repo so the agent's
  // `git checkout --detach <sha>` resolves against a real commit.
  const headSha = await gitHeadSha("acme", "review-happy");
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({
      repo: "acme/review-happy",
      number: 101,
      title: "Real-agent review: happy path",
      body: "Cross-plane e2e: real Go agent + fake-claude happy scenario.",
      headSha,
    }),
  });

  // Ticket appears in the list within a few SSE/polling ticks.
  await page.goto(`${YAAOS_URL}/org/acme/tickets`);
  await expect(page.getByTestId("tickets-list")).toContainText(
    "Real-agent review: happy path",
    { timeout: 20_000 },
  );

  // Navigate to the ticket detail and wait for the review to complete.
  await page.getByText("Real-agent review: happy path").click();
  await expect(page.getByTestId("ticket-detail")).toBeVisible();

  // The real cross-plane signal: fake-github received at least one finding
  // comment posted by yaaos after the real agent ran fake-claude.
  // The posted comment body must contain `no-unused-vars` — the rule_violated
  // field from happy.jsonl — which is only producible by the real agent path
  // (the stub emits `stub/sample-suggestion`).
  await expect
    .poll(
      async () => {
        const comments = await postedComments();
        return comments.some(
          (c) =>
            typeof c.body === "string" && c.body.includes("no-unused-vars"),
        );
      },
      { timeout: 60_000, intervals: [2_000] },
    )
    .toBe(true);
});

/**
 * SSE-driven ticket list update: the tickets list shows the new ticket via
 * SSE without a manual reload. Driven over the same real-agent path as the
 * happy test above so the agent is exercised only once.
 *
 * This is deliberately lightweight — it pairs with the test above (same repo,
 * same agent scenario) and only asserts the SSE-driven list update, not the
 * finding content (which the test above already covers).
 */
test("SSE-driven ticket list shows new ticket without page reload", async ({
  page,
  request,
}) => {
  await setupAuthedAcmeOwner(page, request);

  // Land on the tickets list FIRST so the SSE subscriber is mounted before
  // any events fly.
  await page.goto(`${YAAOS_URL}/org/acme/tickets`);
  const headSha = await gitHeadSha("acme", "review-happy");
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({
      repo: "acme/review-happy",
      number: 102,
      title: "SSE live update: real-agent path",
      headSha,
    }),
  });
  // The ticket must appear without a manual reload — the SSE event
  // `ticket_status_changed` invalidates the tickets query.
  await expect(page.getByText("SSE live update: real-agent path")).toBeVisible({
    timeout: 20_000,
  });
});
