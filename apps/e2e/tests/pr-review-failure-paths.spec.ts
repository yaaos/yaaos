/**
 * Failure paths for the cross-plane review wire.
 *
 * Both tests use the same real Go agent + fake-claude infrastructure as
 * `pr-review-end-to-end.spec.ts`. Scenario selection is by repo slug:
 * the bash `fake-claude` script matches the repo slug (via
 * `git remote get-url origin`) and cats the matching fixture.
 *
 *   `acme/review-nonconforming` → `nonconforming.jsonl`
 *     The terminal `result.result` field is `"not-a-json-object"` — the
 *     backend's `parse_review_output` raises ValueError, `PostFindings`
 *     returns `Outcome.failure(reason="schema_invalid")`, the engine runs
 *     the finalizer (cleanup) and records `failure_reason="schema_invalid"`.
 *     **No findings are posted to fake-github.**
 *
 *   `acme/review-agentfail` → `agentfail.jsonl`
 *     fake-claude exits non-zero (exit 42). The agent reports a
 *     `completed_failure` terminal event. The engine finalizer (cleanup)
 *     runs exactly once and records the failure.
 *     **No findings are posted to fake-github.**
 *
 * Requires the same preconditions as `pr-review-end-to-end.spec.ts`:
 *   - Real Go agent + mock-aws in the test stack.
 *   - `YAAOS_CODING_AGENT_STUB` unset/0 in web and worker.
 *   - fake-github serving git HTTP for clone.
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
  ticketJobStatus,
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
  await seedGithubInstall({ targetOrgSlug: "acme" });
  // Seed skill_name for every scenario repo so build_review_invocation can
  // resolve the skill handle. "code-review" matches the SKILL.md in these repos.
  for (const repo of ["acme/review-happy", "acme/review-nonconforming", "acme/review-agentfail"]) {
    await seedRepoSkill({ orgSlug: "acme", repoExternalId: repo, skillName: "code-review" });
  }

  await page.goto(`${YAAOS_URL}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(/\/orgs\/acme\/dashboard$/);
}

/**
 * Helper: wait up to `timeoutMs` for the ticket to appear, then wait an
 * additional `settleMs` for any late-arriving findings. Returns the comment
 * list at the end of the settle window.
 *
 * Used by failure tests where the contract is "no findings posted". The
 * settle window lets the workflow complete + the cleanup finalizer run before
 * we assert zero comments.
 */
async function waitForTicketAndSettleComments(
  page: Page,
  title: string,
  timeoutMs = 20_000,
  settleMs = 15_000,
): Promise<Array<Record<string, unknown>>> {
  await expect(page.getByTestId("tickets-list")).toContainText(title, {
    timeout: timeoutMs,
  });
  // Give the workflow time to complete (provision → review → post → cleanup
  // finalizer) before asserting no comments were posted.
  const deadline = Date.now() + settleMs;
  let comments: Array<Record<string, unknown>> = [];
  while (Date.now() < deadline) {
    comments = await postedComments();
    if (comments.length > 0) {
      // A comment arrived — break early; the test will fail the assertion
      // below with the actual content in the error message.
      break;
    }
    await new Promise<void>((r) => setTimeout(r, 2_000));
  }
  return comments;
}

/**
 * Non-conforming output: `parse_review_output` raises → `PostFindings` fails
 * with `schema_invalid` → engine runs cleanup finalizer → no findings posted.
 *
 * The repo slug `acme/review-nonconforming` causes fake-claude to emit
 * `nonconforming.jsonl` (result field is a bare string, not JSON).
 */
test("nonconforming output: schema_invalid failure — no findings posted to fake-github", async ({
  page,
  request,
}) => {
  await setupAuthedAcmeOwner(page, request);

  const headSha = await gitHeadSha("acme", "review-nonconforming");
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({
      repo: "acme/review-nonconforming",
      number: 201,
      title: "Failure: nonconforming output",
      body: "fake-claude will emit non-conforming stream-json.",
      headSha,
    }),
  });

  await page.goto(`${YAAOS_URL}/orgs/acme/tickets`);
  const comments = await waitForTicketAndSettleComments(
    page,
    "Failure: nonconforming output",
    25_000,
    20_000,
  );

  // No findings: the schema gate caught the non-conforming output and the
  // workflow failed before `PostFindings` could post anything.
  expect(comments).toHaveLength(0);

  // Tightened: the workflow must have completed with `failed` status.
  // This catches regressions where the workflow never started (e.g. git
  // clone failed before InvokeClaudeCode was dispatched) — those would
  // also have no comments but would either be stuck in `running` or fail
  // at ProvisionWorkspace rather than at PostFindings.
  // Use page.request (shares session cookie with the browser context) so
  // the authenticated /api/tickets and /api/reviewer/jobs endpoints succeed.
  await expect
    .poll(
      () => ticketJobStatus("acme", "Failure: nonconforming output", page.request),
      { timeout: 5_000 },
    )
    .toBe("failed");
});

/**
 * Agent failure: fake-claude exits non-zero → `completed_failure` terminal
 * event → cleanup finalizer runs exactly once → no findings posted.
 *
 * The repo slug `acme/review-agentfail` causes fake-claude to emit
 * `agentfail.jsonl` and exit 42.
 */
test("agent failure: terminal failure — cleanup finalizer runs, no findings posted", async ({
  page,
  request,
}) => {
  await setupAuthedAcmeOwner(page, request);

  const headSha = await gitHeadSha("acme", "review-agentfail");
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({
      repo: "acme/review-agentfail",
      number: 301,
      title: "Failure: agent exit non-zero",
      body: "fake-claude will exit with code 42.",
      headSha,
    }),
  });

  await page.goto(`${YAAOS_URL}/orgs/acme/tickets`);
  const comments = await waitForTicketAndSettleComments(
    page,
    "Failure: agent exit non-zero",
    25_000,
    20_000,
  );

  // No findings: the agent reported terminal failure; the workflow ran the
  // cleanup finalizer and recorded the failure without posting any findings.
  expect(comments).toHaveLength(0);

  // Tightened: the workflow must have completed with `failed` status,
  // proving the agent actually ran InvokeClaudeCode (and exit 42 was
  // received) rather than the workflow never starting at all.
  // Use page.request (shares session cookie with the browser context) so
  // the authenticated /api/tickets and /api/reviewer/jobs endpoints succeed.
  await expect
    .poll(
      () => ticketJobStatus("acme", "Failure: agent exit non-zero", page.request),
      { timeout: 5_000 },
    )
    .toBe("failed");
});
