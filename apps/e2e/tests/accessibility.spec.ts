/**
 * Accessibility — WCAG 2.1 AA via axe-core on every M06 anchor.
 *
 * Asserts the four redesigned anchors (Dashboard / Tickets list / Ticket
 * detail / Coding Agent detail) leave a no-violation paper trail. M06's
 * F2 § D promised "axe-core clean in CI for every page-level E2E test."
 *
 * Each test reuses one stack-bring-up via Playwright's test.describe
 * shared `beforeEach` for auth + install seeding.
 */

import AxeBuilder from "@axe-core/playwright";
import { type APIRequestContext, type Page, expect, test } from "@playwright/test";

import { dispatchWebhook, prPayload, resetStack, seedGithubInstall } from "./_helpers";

const BASE = process.env.YAAOS_BASE_URL ?? "http://localhost:58080";

async function loginAsOwner(page: Page, request: APIRequestContext) {
  await resetStack();
  await request.post(`${BASE}/api/testing/seed/bootstrap_owner`, {
    data: {
      email: "owner@yaaos.test",
      github_id: "1001",
      org_slug: "acme",
      display_name: "Owner",
      provider: "test",
    },
  });
  await request.post(`${BASE}/api/testing/oauth_test/stage_profile`, {
    data: {
      external_subject: "1001",
      primary_email: "owner@yaaos.test",
      email_verified: true,
      display_name: "Owner",
    },
  });
  // The Owner needs valid credentials + an installed Coding Agent so the
  // Coding Agent settings detail page has something to render against.
  await seedGithubInstall({ targetOrgSlug: "acme" });
  await page.goto(`${BASE}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(/\/orgs\/acme\/dashboard$/);
}

async function expectNoViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
}

test.describe("a11y — M06 anchors", () => {
  test("Dashboard has no WCAG AA violations", async ({ page, request }) => {
    await loginAsOwner(page, request);
    await expectNoViolations(page);
  });

  test("Tickets list has no WCAG AA violations", async ({ page, request }) => {
    await loginAsOwner(page, request);
    // Seed one ticket via the real webhook path so the list isn't empty —
    // axe-checking an empty page hides bugs in row markup.
    await dispatchWebhook({
      event: "pull_request",
      payload: prPayload({
        repo: "acme/api",
        number: 101,
        title: "A11y fixture ticket",
        body: "Seeded so axe can scan a populated tickets table.",
      }),
    });
    await page.goto(`${BASE}/orgs/acme/tickets`);
    await expect(page.getByTestId("tickets-list")).toContainText("A11y fixture ticket", {
      timeout: 20_000,
    });
    await expectNoViolations(page);
  });

  test("Ticket detail has no WCAG AA violations", async ({ page, request }) => {
    await loginAsOwner(page, request);
    await dispatchWebhook({
      event: "pull_request",
      payload: prPayload({
        repo: "acme/api",
        number: 102,
        title: "A11y detail fixture",
        body: "Seeded so axe can scan the detail page composites.",
      }),
    });
    await page.goto(`${BASE}/orgs/acme/tickets`);
    await page.getByText("A11y detail fixture").click({ timeout: 20_000 });
    await expect(page.getByTestId("ticket-detail")).toBeVisible();
    await expectNoViolations(page);
  });

  test("Coding Agent detail has no WCAG AA violations", async ({ page, request }) => {
    await loginAsOwner(page, request);
    await page.goto(`${BASE}/orgs/acme/settings/coding-agents/claude_code`);
    // Wait for the AgentEditor's testid to confirm the bespoke UI mounted
    // (not the "not installed" placeholder).
    await expect(page.getByTestId("cc-save")).toBeVisible({ timeout: 10_000 });
    await expectNoViolations(page);
  });
});
