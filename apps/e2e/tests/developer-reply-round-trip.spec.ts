/**
 * Plan §12: developer reply round-trip.
 *
 * PR opens → yaaos posts a review with one finding → developer replies with a
 * high-confidence wontfix → classifier returns `acknowledgment` ≥ 0.85 →
 * aggregate flips the finding to `acknowledged` and yaaos posts the canned
 * "Noted — I'll skip this in future reviews." reply → the UI reflects the
 * acknowledged state in the durable-findings + all-conversations sections,
 * and an `finding_acknowledged` audit row exists.
 *
 * Boundary: a single `pull_request_review_comment.created` webhook from
 * fake-github → yaaos. The reply body is chosen to be unambiguous so the
 * real classifier reliably scores it at the act-threshold.
 *
 * NOTE on infra: as of this writing, the e2e docker stack has no LLM
 * credentials (no `BRAINTRUST_API_KEY` / `ANTHROPIC_API_KEY` in
 * `docker/docker-compose.test.yml`). `reviewer.replies.handle_developer_reply`
 * calls `classify_reply` directly via `core/llm`, which routes through
 * LangChain's provider clients — those need a key. There is no e2e-only
 * classifier stub and no `/api/testing/seed/classifier_response` surface.
 * Until one of those exists this spec cannot run green in CI. See the
 * accompanying handoff note for the missing infra.
 */

import { expect, test } from "@playwright/test";
import {
  YAAOS_URL,
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

test("developer wontfix reply → finding acknowledged → yaaos posts reply + UI reflects state", async ({
  page,
  request,
}) => {
  // 1. PR opens; yaaos runs an initial review and posts one inline finding
  //    (the stub coding agent always emits one synthetic FindingDraft on
  //    `src/example.ts:1`, posted via the inline-comments endpoint).
  await dispatchWebhook({
    event: "pull_request",
    payload: prPayload({
      repo: "acme/api",
      number: 88,
      title: "Wire metrics middleware",
    }),
  });

  await page.goto("/tickets");
  await page.getByText("Wire metrics middleware").click();
  await expect
    .poll(() => page.locator('[data-testid^="agent-card-"][data-state="posted"]').count(), {
      timeout: 30_000,
    })
    .toBe(1);

  // 2. Grab the id of the inline comment yaaos posted — that's the parent
  //    the developer will reply to. The stub finding is inline so there is
  //    exactly one inline `posted_comments` entry with `path`/`line` set.
  const before = await postedComments();
  const yaaosFindingComment = before.find(
    (c) => typeof c.path === "string" && typeof c.line === "number",
  );
  expect(yaaosFindingComment, "yaaos posted at least one inline finding comment").toBeDefined();
  const parentExternalId = String(yaaosFindingComment!.id);
  const commentsBefore = before.length;

  // 3. Developer replies with an unambiguous high-confidence wontfix.
  //    Plan §10.3 act-band is ≥ 0.85; this body is intentionally explicit
  //    so the classifier scores it well above that floor without any
  //    canned response shim.
  const replyBody =
    "wontfix; intentional design choice — we throw early on missing config and the caller handles it. Not changing.";
  const developerCommentId = `gh-dev-reply-${Date.now()}`;
  await dispatchWebhook({
    event: "pull_request_review_comment",
    payload: {
      action: "created",
      pull_request: { number: 88 },
      repository: { full_name: "acme/api" },
      installation: { id: "fake-install-1" },
      comment: {
        id: developerCommentId,
        body: replyBody,
        in_reply_to_id: parentExternalId,
        pull_request_review_id: null,
        user: { login: "alice", type: "User" },
      },
    },
  });

  // 4. yaaos posts the canned ack reply back to fake-github via the
  //    inline-comments `replies` endpoint. The reply body is fixed in
  //    `reviewer/service.py::apply_classified_reply`.
  await expect
    .poll(
      async () => {
        const after = await postedComments();
        return after
          .slice(commentsBefore)
          .some(
            (c) => typeof c.body === "string" && c.body.startsWith("Noted — I'll skip this"),
          );
      },
      { timeout: 30_000 },
    )
    .toBe(true);

  // 5. Look up the ticket id so we can hit the audit + findings APIs.
  const ticketsResp = await request.get(`${YAAOS_URL}/api/tickets`);
  const tickets = (await ticketsResp.json()) as Array<{ id: string; title: string }>;
  const ticket = tickets.find((t) => t.title === "Wire metrics middleware");
  expect(ticket).toBeDefined();
  const ticketId = ticket!.id;

  // 6. Audit log carries a `finding_acknowledged` row.
  await expect
    .poll(
      async () => {
        const r = await request.get(`${YAAOS_URL}/api/tickets/${ticketId}/audit`);
        const audit = (await r.json()) as Array<{ kind: string }>;
        return audit.some((e) => e.kind === "finding_acknowledged");
      },
      { timeout: 15_000 },
    )
    .toBe(true);

  // 7. Findings API reports the finding as `acknowledged`.
  await expect
    .poll(
      async () => {
        const r = await request.get(
          `${YAAOS_URL}/api/reviewer/findings/by-ticket/${ticketId}?include_terminal=true`,
        );
        const findings = (await r.json()) as Array<{ state: string }>;
        return findings.some((f) => f.state === "acknowledged");
      },
      { timeout: 10_000 },
    )
    .toBe(true);

  // 8. UI reflects it. The "All Conversations" cross-cut surfaces the
  //    finding (plan §9.3); the durable-findings list (with terminal
  //    states toggled on) shows the StatePill in the `acknowledged` tone.
  await page.reload();
  await expect(page.getByTestId("all-conversations")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId("all-conversations").getByText("acknowledged")).toBeVisible();

  // Expand the finding row to see the thread; verify both messages render.
  await page.getByTestId("finding-timeline-row").first().click();
  const threadPanel = page.getByTestId("finding-thread-panel").first();
  await expect(threadPanel).toBeVisible();
  await expect(threadPanel.getByTestId("human-message")).toContainText("wontfix");
  await expect(threadPanel.getByTestId("yaaos-message").last()).toContainText("Noted");
  await expect(threadPanel.getByTestId("ack-banner")).toBeVisible();
});
