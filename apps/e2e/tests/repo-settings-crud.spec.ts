/**
 * Org Settings > Repos — admin configures a repo in the browser.
 *
 * Binds `github:pr_opened` to a seeded pipeline on `acme/web` and sees the
 * trigger chip, flips protected-code mode and sees the inversion confirm,
 * then saves a path set with an owner and confirms it round-trips through
 * a reload. A repo with no config renders the `unconfigured` badge, which
 * disappears once any of triggers/protected-code/auto-approve is set.
 *
 * Requires the live Docker stack (`bin/dev-rebuild`).
 */

import { expect, test } from "@playwright/test";
import { resetStack, seedGithubInstall, YAAOS_URL } from "./_helpers";

const ORG_SLUG = "acme-repo-settings";

test("admin binds a trigger, flips protected-code mode with confirm, and saves a path set with an owner", async ({
  page,
  request,
}) => {
  await resetStack();
  const bootstrap = await request.post(`${YAAOS_URL}/api/testing/seed/bootstrap_owner`, {
    data: {
      email: "owner@repo-settings.test",
      github_id: "9101",
      org_slug: ORG_SLUG,
      display_name: "Owner",
      provider: "test",
    },
  });
  const { org_id: orgId, user_id: ownerId } = (await bootstrap.json()) as {
    org_id: string;
    user_id: string;
  };

  await request.post(`${YAAOS_URL}/api/testing/oauth_test/stage_profile`, {
    data: {
      external_subject: "9101",
      primary_email: "owner@repo-settings.test",
      email_verified: true,
      display_name: "Owner",
    },
  });
  await seedGithubInstall({ targetOrgSlug: ORG_SLUG });

  const pipelineResp = await request.post(`${YAAOS_URL}/api/testing/seed/pipeline`, {
    data: { org_id: orgId, name: "dev" },
  });
  expect(pipelineResp.ok()).toBe(true);

  await page.goto(`${YAAOS_URL}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(new RegExp(`/org/${ORG_SLUG}/workspaces$`));

  await page.goto(`${YAAOS_URL}/org/${ORG_SLUG}/settings/repos`);
  await expect(page.getByTestId("repos-list")).toBeVisible();

  const repoRow = page.getByTestId("repo-row-acme/web");
  await expect(repoRow).toBeVisible();
  await expect(repoRow.getByTestId("repo-row-acme/web-status")).toHaveText("unconfigured");

  await repoRow.getByText("acme/web").click();
  await expect(page.getByTestId("repo-config-acme/web")).toBeVisible();

  // Bind github:pr_opened → the seeded "dev" pipeline.
  await page.getByTestId("repo-add-trigger").click();
  const triggerForm = page.getByTestId("repo-trigger-form");
  await triggerForm.getByTestId("repo-trigger-intake-point").click();
  await page.getByRole("option", { name: "PR opened" }).click();
  await triggerForm.getByTestId("repo-trigger-pipeline").click();
  await page.getByRole("option", { name: "dev" }).click();
  await triggerForm.getByTestId("repo-trigger-save").click();
  await expect(triggerForm).toBeHidden();
  await expect(repoRow.getByText("dev")).toBeVisible();

  // The unconfigured badge is gone now that a trigger exists.
  await expect(repoRow.getByTestId("repo-row-acme/web-status")).toHaveCount(0);

  // Protected-code mode-switch confirm — flipping deny→allow requires it.
  await expect(page.getByRole("radio", { name: /Deny list/ })).toBeChecked();
  await page.getByRole("radio", { name: /Allow list/ }).click();
  const confirmDialog = page.getByTestId("repo-protected-mode-confirm");
  await expect(confirmDialog).toBeVisible();
  await expect(confirmDialog).toContainText("This inverts what's protected.");
  await confirmDialog.getByTestId("repo-protected-mode-confirm-switch").click();
  await expect(page.getByRole("radio", { name: /Allow list/ })).toBeChecked();

  // Add a path set with an owner and save.
  await page.getByTestId("repo-add-path-set").click();
  await page
    .locator('[data-testid^="repo-path-set-globs-"]')
    .fill("src/migrations/**\ninfra/**");
  await page.locator('[data-testid^="repo-path-set-owners-"]').click();
  await page.locator(`[data-testid$="-option-${ownerId}"]`).click();
  await page.getByTestId("repo-settings-save").click();
  await expect(page.getByText("Saved.")).toBeVisible({ timeout: 10_000 });

  // Round-trips through `GET /api/repos/config` — reload, re-expand, re-read.
  await page.reload();
  await page.getByTestId("repo-row-acme/web").getByText("acme/web").click();
  await expect(page.getByTestId("repo-config-acme/web")).toBeVisible();
  await expect(page.getByRole("radio", { name: /Allow list/ })).toBeChecked();
  await expect(page.locator('[data-testid^="repo-path-set-globs-"]')).toHaveValue(
    "src/migrations/**\ninfra/**",
  );
});
