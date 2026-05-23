/**
 * Accessibility — WCAG 2.1 AA via axe-core.
 *
 * M06 D4 baseline: every page-level e2e ought to leave a no-violation paper trail.
 * Phase 1 ships this one spec against the existing Dashboard so the rule lands
 * before any new surface is redesigned. Subsequent phases add their own page
 * after-redesign — the failure mode we want is "the redesigned page violated a
 * rule," not "we never noticed because no test asserted it."
 */

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const BASE = process.env.YAAOS_BASE_URL ?? "http://localhost:58080";

async function loginAsOwner(page: import("@playwright/test").Page, request: import("@playwright/test").APIRequestContext) {
  await request.post(`${BASE}/api/testing/reset`);
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
  await page.goto(`${BASE}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(/\/orgs\/acme\/dashboard$/);
}

test.describe("a11y", () => {
  test("dashboard has no WCAG AA violations", async ({ page, request }) => {
    await loginAsOwner(page, request);

    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();

    expect(results.violations).toEqual([]);
  });
});
