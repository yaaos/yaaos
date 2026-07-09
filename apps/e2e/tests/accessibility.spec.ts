/**
 * Accessibility — WCAG 2.1 AA via axe-core on every anchor page.
 *
 * Asserts the four redesigned anchors (Dashboard / Tickets list / Ticket
 * detail / Coding Agent detail) leave a no-violation paper trail.
 * F2 § D promised "axe-core clean in CI for every page-level E2E test."
 *
 * Each test reuses one stack-bring-up via Playwright's test.describe
 * shared `beforeEach` for auth + install seeding.
 */

import AxeBuilder from "@axe-core/playwright";
import { type Page, expect, test } from "@playwright/test";

import { YAAOS_URL, loginAsOwner, seedPausedRun } from "./_helpers";

async function expectNoViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
}

test.describe("a11y — anchor pages", () => {
  test("Workspaces has no WCAG AA violations", async ({ page, request }) => {
    await loginAsOwner(page, request);
    await expectNoViolations(page);
  });

  test("Tickets list has no WCAG AA violations", async ({ page, request }) => {
    await loginAsOwner(page, request);
    // Seed one ticket (paused pipeline run) so the list isn't empty —
    // axe-checking an empty page hides bugs in row markup.
    await seedPausedRun({ orgSlug: "acme", ticketTitle: "A11y fixture ticket" });
    await page.goto(`${YAAOS_URL}/org/acme/tickets`);
    await expect(page.getByTestId("tickets-list")).toContainText("A11y fixture ticket", {
      timeout: 20_000,
    });
    await expectNoViolations(page);
  });

  test("Ticket detail has no WCAG AA violations", async ({ page, request }) => {
    await loginAsOwner(page, request);
    await seedPausedRun({ orgSlug: "acme", ticketTitle: "A11y detail fixture" });
    await page.goto(`${YAAOS_URL}/org/acme/tickets`);
    await page.getByText("A11y detail fixture").click({ timeout: 20_000 });
    await expect(page.getByTestId("ticket-detail")).toBeVisible();
    await expectNoViolations(page);
  });

  test("Coding Agent detail has no WCAG AA violations", async ({ page, request }) => {
    await loginAsOwner(page, request);
    await page.goto(`${YAAOS_URL}/org/acme/settings/coding-agents/claude_code`);
    // Wait for the uninstall button to confirm the bespoke UI mounted
    // (not the "not installed" placeholder).
    await expect(page.getByTestId("cc-uninstall-button")).toBeVisible({ timeout: 10_000 });
    await expectNoViolations(page);
  });
});
