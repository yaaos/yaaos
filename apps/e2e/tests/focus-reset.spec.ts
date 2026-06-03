/**
 * Focus-reset on route navigation — WCAG 2.4.3 (focus order) and 2.4.1
 * (bypass blocks).
 *
 * On every client-side navigation the SPA must move keyboard focus to the
 * first <h1> in <main> (if present) or to <main> itself, so screen-reader
 * and keyboard users land at the top of the new page.
 */

import { expect, test } from "@playwright/test";
import { YAAOS_URL, loginAsOwner } from "./_helpers";

// Runs in the browser: classifies where focus currently sits relative to
// <main>. Returns "main", "h1" (a heading inside main), or "other:<tag>".
function focusPlacement(): string {
  const el = document.activeElement as HTMLElement | null;
  if (!el) return "none";
  const main = document.querySelector("main");
  if (el === main) return "main";
  if (el.tagName.toLowerCase() === "h1" && main?.contains(el)) return "h1";
  return `other:${el.tagName.toLowerCase()}`;
}

test.describe("focus-reset on route navigation", () => {
  test("navigating from Dashboard to Tickets moves focus to <main> or <h1>", async ({
    page,
    request,
  }) => {
    await loginAsOwner(page, request);
    // Dashboard: focus settles inside <main> once the route is idle. Poll —
    // focus-reset is deferred a frame past navigation, so it is inherently async.
    await expect
      .poll(() => page.evaluate(focusPlacement), {
        message: "focus should land on <main> or <h1> on initial page",
      })
      .toMatch(/^(main|h1)$/);

    // Navigate to Tickets.
    await page.getByRole("link", { name: /tickets/i }).first().click();
    await page.waitForURL(/\/orgs\/acme\/tickets/);

    // After navigation: focus must be on <main> or the <h1> inside it.
    await expect
      .poll(() => page.evaluate(focusPlacement), {
        message: "expected focus on main or h1 after nav",
      })
      .toMatch(/^(main|h1)$/);
  });

  test("navigating from Tickets to Lessons moves focus to <main> or <h1>", async ({
    page,
    request,
  }) => {
    await loginAsOwner(page, request);
    await page.goto(`${YAAOS_URL}/orgs/acme/tickets`);
    await page.waitForURL(/\/orgs\/acme\/tickets/);

    // Move focus to the sidebar so we can assert it moves after navigation.
    await page.getByRole("link", { name: /lessons/i }).focus();

    await page.getByRole("link", { name: /lessons/i }).click();
    await page.waitForURL(/\/orgs\/acme\/lessons/);

    await expect
      .poll(() => page.evaluate(focusPlacement), {
        message: "expected focus on main or h1 after nav",
      })
      .toMatch(/^(main|h1)$/);
  });
});
