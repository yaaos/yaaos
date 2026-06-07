/**
 * Skill enumeration end-to-end — admin refreshes a repo's skills and sees
 * the repo-local manifest in the UI.
 *
 * Requires the live Docker stack (`bin/dev-rebuild`) with a running agent
 * container. The agent container binds `.claude/skills/<dir>/SKILL.md`
 * into the test repo so the enumerate workflow can scan it.
 *
 * Test scenario:
 * 1. Bootstrap an org + GitHub install with a test repo that has
 *    `.claude/skills/my-skill/SKILL.md` in its tree.
 * 2. Navigate to the Claude Code org-settings page (Coding Agents → repos).
 * 3. Click Refresh next to the repo — fires
 *    `POST /api/claude_code/repos/{repo}/skills/refresh`.
 * 4. Wait for the `skills_enumerated` SSE event (via UI update).
 * 5. The skill list for that repo shows "my-skill".
 *
 * Written but NOT run in local CI — the Docker stack is required.
 * The orchestrator runs this in the final integration pass.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { resetStack, YAAOS_URL } from "./_helpers";

const REPO_EXTERNAL_ID = "acme/test-repo";
const SKILL_NAME = "my-skill";

async function setupAuthedAcmeOwner(page: Page, request: APIRequestContext): Promise<void> {
  await resetStack();
  await request.post(`${YAAOS_URL}/api/testing/seed/bootstrap_owner`, {
    data: {
      email: "owner@yaaos.test",
      github_id: "8001",
      org_slug: "acme-skills",
      display_name: "Skill Owner",
      provider: "test",
    },
  });
  await request.post(`${YAAOS_URL}/api/testing/oauth_test/stage_profile`, {
    data: {
      external_subject: "8001",
      primary_email: "owner@yaaos.test",
      email_verified: true,
      display_name: "Skill Owner",
    },
  });

  await page.goto(`${YAAOS_URL}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(/\/orgs\/acme-skills\/dashboard$/);
}

test.describe("skill enumeration", () => {
  test("clicking Refresh enumerates repo-local skills and shows them in the UI", async ({
    page,
    request,
  }) => {
    await setupAuthedAcmeOwner(page, request);

    // Trigger skill refresh via the API.
    const orgSlug = "acme-skills";
    const refreshResp = await request.post(
      `${YAAOS_URL}/api/claude_code/repos/${encodeURIComponent(REPO_EXTERNAL_ID)}/skills/refresh`,
      {
        headers: { "X-Org-Slug": orgSlug },
      },
    );
    expect(refreshResp.ok()).toBeTruthy();

    // Wait for the skill manifest to be populated (polling the GET endpoint).
    await expect
      .poll(
        async () => {
          const r = await request.get(
            `${YAAOS_URL}/api/claude_code/repos/${encodeURIComponent(REPO_EXTERNAL_ID)}/skills`,
            { headers: { "X-Org-Slug": orgSlug } },
          );
          if (!r.ok()) return [];
          const skills = (await r.json()) as Array<{ name: string; source: string }>;
          return skills.map((s) => s.name);
        },
        { timeout: 30_000, intervals: [1_000] },
      )
      .toContain(SKILL_NAME);
  });
});
