/**
 * Org Settings > Pipelines — admin composes a pipeline in the browser.
 *
 * Creates a pipeline from the `dev` template (which also materializes its
 * called `implementation` pipeline), edits a stage's boundary to
 * always-proceed — "Save stage" auto-saves the definition immediately
 * ("Saved." status) and the edit persists across a page reload —
 * introduces a call cycle (`implementation` → `dev` → `implementation`)
 * whose auto-save surfaces the `invalid_definition` banner, then deletes
 * the referenced `implementation` pipeline and sees the "In use…" message.
 *
 * Requires the live Docker stack (`bin/dev-rebuild`).
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { loginAsOwner, resetStack, YAAOS_URL } from "./_helpers";

const ORG_SLUG = "acme-pipeline-settings";
const BUILDER_GITHUB_ID = "9002";

test("admin creates a pipeline from a template, auto-saves a boundary edit, hits a cycle, and deletes a referenced pipeline", async ({
  page,
  request,
}) => {
  await loginAsOwner(page, request, ORG_SLUG);

  await page.goto(`${YAAOS_URL}/org/${ORG_SLUG}/settings/pipelines`);
  await expect(page.getByTestId("pipeline-new-from-template")).toBeVisible();

  // Create from the "dev" template — instantiate_template deep-copies its
  // called "implementation" pipeline too, so both rows appear.
  await page.getByTestId("pipeline-new-from-template").click();
  const templateDialog = page.getByTestId("pipeline-template-dialog");
  await expect(templateDialog).toBeVisible();
  await templateDialog.getByTestId("pipeline-template-dev").click();

  const list = page.getByTestId("pipelines-list");
  await expect(list).toBeVisible();
  // Scope to each row's own accordion-trigger button, not the row's full
  // text — an expanded row's stage list can itself render a *sibling*
  // pipeline's name (e.g. `dev`'s `call` stage summarizes as
  // "implementation"), which would make a whole-row `hasText` filter
  // ambiguous between the two rows.
  const pipelineRow = (name: string) =>
    list
      .locator('[data-testid^="pipeline-row-"]')
      .filter({ has: page.getByRole("button", { name }) });
  const devRow = pipelineRow("dev");
  await expect(devRow).toBeVisible({ timeout: 15_000 });
  const implementationRow = pipelineRow("implementation");
  await expect(implementationRow).toBeVisible();

  // Edit a stage's boundary to always-proceed — "Save stage" auto-saves the
  // whole definition immediately.
  await devRow.getByRole("button", { name: "dev" }).click();
  await expect(devRow.getByTestId(/^pipeline-stage-edit-/).first()).toBeVisible();
  await devRow.getByTestId(/^pipeline-stage-edit-/).first().click();

  const stageEditor = page.getByTestId("stage-editor");
  await expect(stageEditor).toBeVisible();
  await stageEditor.getByRole("radio", { name: "Always proceed automatically" }).click();
  await stageEditor.getByTestId("stage-editor-save").click();
  await expect(stageEditor).toBeHidden();

  // Auto-save round-trips ("Saving…" → "Saved.") with no error banner.
  await expect(devRow.getByTestId("pipeline-save-status")).toHaveText("Saved.", {
    timeout: 10_000,
  });
  await expect(devRow.getByText(/Couldn't save|Invalid pipeline definition/)).toHaveCount(0);

  // The boundary edit persists across a reload — reopen the stage editor and
  // the always-proceed radio is still checked.
  await page.reload();
  await expect(devRow).toBeVisible({ timeout: 15_000 });
  await devRow.getByRole("button", { name: "dev" }).click();
  await expect(devRow.getByTestId(/^pipeline-stage-edit-/).first()).toBeVisible();
  await devRow.getByTestId(/^pipeline-stage-edit-/).first().click();
  await expect(stageEditor).toBeVisible();
  await expect(
    stageEditor.getByRole("radio", { name: "Always proceed automatically" }),
  ).toBeChecked();
  await stageEditor.getByRole("button", { name: "Cancel" }).click();
  await expect(stageEditor).toBeHidden();

  // Introduce a call cycle: implementation → dev (dev already calls
  // implementation) → the stage save's auto-save PUT returns 400
  // invalid_definition.
  await implementationRow.getByRole("button", { name: "implementation" }).click();
  await expect(implementationRow.getByTestId("pipeline-add-stage")).toBeVisible();
  await implementationRow.getByTestId("pipeline-add-stage").click();
  await page.getByTestId("pipeline-add-stage-call").click();

  const callStageEditor = page.getByTestId("stage-editor");
  await expect(callStageEditor).toBeVisible();
  await callStageEditor.getByTestId("stage-call-pipeline").click();
  await page.getByRole("option", { name: "dev" }).click();
  await callStageEditor.getByTestId("stage-editor-save").click();
  await expect(callStageEditor).toBeHidden();

  await expect(implementationRow.getByText(/Invalid pipeline definition/)).toBeVisible({
    timeout: 10_000,
  });

  // Deleting the referenced "implementation" pipeline (dev's call stage
  // still targets it) surfaces the in-use message, not a silent 204.
  await implementationRow.getByTestId("pipeline-delete").click();
  const confirmDialog = page.getByRole("dialog", { name: /Delete implementation\?/ });
  await expect(confirmDialog).toBeVisible();
  await confirmDialog.getByRole("button", { name: "Delete" }).click();
  await expect(
    implementationRow.getByText("In use by a repo trigger or another pipeline."),
  ).toBeVisible({ timeout: 10_000 });
});

async function loginAsBuilder(page: Page): Promise<void> {
  await page.request.post(`${YAAOS_URL}/api/testing/oauth_test/stage_profile`, {
    data: {
      external_subject: BUILDER_GITHUB_ID,
      primary_email: "builder@pipeline-settings.test",
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
      email: "builder@pipeline-settings.test",
      github_id: BUILDER_GITHUB_ID,
      role: "builder",
      display_name: "Builder",
      provider: "test",
    },
  });
}

test("a builder sees no Pipelines link in Org Settings", async ({ page, request }) => {
  await resetStack();
  await request.post(`${YAAOS_URL}/api/testing/seed/bootstrap_owner`, {
    data: {
      email: "owner@pipeline-settings.test",
      github_id: "9001",
      org_slug: ORG_SLUG,
      display_name: "Owner",
      provider: "test",
    },
  });
  await seedBuilder(request);
  await loginAsBuilder(page);

  await expect(page.getByTestId("nav-group-org-settings")).toBeVisible();
  await page.getByTestId("nav-group-org-settings").click();
  await expect(page.getByTestId("nav-members")).toBeVisible();
  await expect(page.getByTestId("nav-pipelines")).not.toBeVisible();

  // Direct navigation doesn't render the page's contents either — the
  // backend's `PIPELINES_MANAGE` (admin) gate is the real authority.
  await page.goto(`${YAAOS_URL}/org/${ORG_SLUG}/settings/pipelines`);
  await expect(page.getByTestId("pipeline-new-from-template")).not.toBeVisible();
});
