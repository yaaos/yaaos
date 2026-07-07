/**
 * Ticket page Overview tab — the attention block for a paused run, and
 * live (SSE, no reload) resolution when an org owner approves it.
 *
 * Boundary: `seedPausedRun` constructs a `paused` pipeline run + open
 * `run_pauses` row directly via the backend's public service layer
 * (`domain/pipelines.start_run`'s real dispatch machinery is bypassed —
 * see `app/testing/e2e_setup/service.py::seed_paused_run` — no real
 * coding-agent invocation is involved). The SPA then drives the real
 * `POST /api/pipelines/runs/pauses/{id}/respond` endpoint end to end.
 *
 * Requires the live Docker stack (`bin/dev-rebuild`).
 */

import { expect, type APIRequestContext, type Page, test } from "@playwright/test";
import { loginAsOwner, resetStack, seedPausedRun, YAAOS_URL } from "./_helpers";

const BUILDER_GITHUB_ID = "8002";
const ORG_SLUG = "acme-pipeline-overview";

async function loginAsBuilder(page: Page): Promise<void> {
  await page.request.post(`${YAAOS_URL}/api/testing/oauth_test/stage_profile`, {
    data: {
      external_subject: BUILDER_GITHUB_ID,
      primary_email: "builder@pipeline-overview.test",
      email_verified: true,
      display_name: "Builder",
    },
  });
  await page.goto(`${YAAOS_URL}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(new RegExp(`/org/${ORG_SLUG}/workspaces$`));
}

async function seedBuilder(request: APIRequestContext): Promise<void> {
  await request.post(`${YAAOS_URL}/api/testing/seed/member_for_org`, {
    data: {
      org_slug: ORG_SLUG,
      email: "builder@pipeline-overview.test",
      github_id: BUILDER_GITHUB_ID,
      role: "builder",
      display_name: "Builder",
      provider: "test",
    },
  });
}

test("paused run renders the attention block; approving continues it live", async ({
  page,
  request,
}) => {
  await loginAsOwner(page, request, ORG_SLUG);

  const seeded = await seedPausedRun({ orgSlug: ORG_SLUG, ticketTitle: "Seeded paused run" });

  await page.goto(`${YAAOS_URL}/org/${ORG_SLUG}/tickets/${seeded.ticket_id}`);
  await expect(page.getByTestId("ticket-detail")).toBeVisible();
  await expect(page.getByTestId("ticket-status-hitl")).toBeVisible();

  const block = page.getByTestId("attention-block");
  await expect(block).toBeVisible();
  await expect(block).toHaveAttribute("data-state", "paused");
  await expect(block).toContainText("write-spec");
  await expect(block).toContainText("always hitl");

  // The seeded owner is an org admin, authorized regardless of the pause's
  // (empty) escalation set — actions are enabled, no reload needed.
  await expect(page.getByTestId("approve-run")).toBeEnabled();

  await page.getByTestId("approve-run").click();

  // SSE `run_state_changed` drives the attention block to a new state
  // without a page reload — no `page.reload()` anywhere in this spec.
  await expect(block).toHaveAttribute("data-state", "completed", { timeout: 20_000 });
  await expect(page.getByTestId("approve-run")).toHaveCount(0);
});

test("a builder who isn't in the escalation set sees the actions disabled", async ({
  page,
  request,
}) => {
  // Seed everything via the API request context — never log the owner into
  // the browser page. The builder (the actor under test) is the only
  // identity that ever calls `page.goto("/login")` in this test; logging
  // in as owner first would leave a session cookie in the page context that
  // short-circuits the next `/login` visit before "login-test" renders.
  await resetStack();
  await request.post(`${YAAOS_URL}/api/testing/seed/bootstrap_owner`, {
    data: {
      email: "owner@pipeline-overview.test",
      github_id: "8001",
      org_slug: ORG_SLUG,
      display_name: "Owner",
      provider: "test",
    },
  });
  await seedBuilder(request);
  const seeded = await seedPausedRun({
    orgSlug: ORG_SLUG,
    ticketTitle: "Paused run, builder view",
  });

  await loginAsBuilder(page);
  await page.goto(`${YAAOS_URL}/org/${ORG_SLUG}/tickets/${seeded.ticket_id}`);

  const block = page.getByTestId("attention-block");
  await expect(block).toBeVisible();
  await expect(block).toHaveAttribute("data-state", "paused");
  // `can_respond` is server-computed per-actor — a plain builder outside the
  // (empty) escalation set is not an org admin, so every action is disabled.
  await expect(page.getByTestId("approve-run")).toBeDisabled();
  await expect(page.getByTestId("kill-run")).toBeDisabled();
  await expect(page.getByTestId("pause-waiting-on")).toBeVisible();
});
