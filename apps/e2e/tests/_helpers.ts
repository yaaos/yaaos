/**
 * Shared utilities for e2e specs.
 *
 * Each spec drives its own preconditions in `beforeEach` by composing these
 * helpers — there is no batch-seeded fixture. Reset wipes yaaos's DB and
 * fake-github's in-memory state, then specs call `seedGithubInstall`
 * if they need the system in a "ready" state.
 *
 * URL envelope:
 *   - `YAAOS_URL`           — yaaos's UI, hit from the browser (default :58080).
 *   - `FAKE_GITHUB_URL`     — fake-github's UI, hit from the test runner.
 *   - `YAAOS_INTERNAL_URL`  — yaaos from inside fake-github's docker network.
 *     Used as `target_url` for webhook dispatch (fake-github → yaaos).
 */

import type { APIRequestContext, Page } from "@playwright/test";

export const YAAOS_URL = process.env.YAAOS_BASE_URL ?? "http://localhost:58080";
export const FAKE_GITHUB_URL = process.env.FAKE_GITHUB_URL ?? "http://localhost:58081";
export const YAAOS_INTERNAL_URL = process.env.YAAOS_INTERNAL_URL ?? "http://web:8080";

async function jsonPost(url: string, body: unknown): Promise<Response> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) {
    throw new Error(`POST ${url} → ${r.status}: ${await r.text()}`);
  }
  return r;
}

/** Reset both yaaos's DB and fake-github's in-memory state to a known floor.
 *  After this call: yaaos DB is fully empty (reviewer specialists are shipped
 *  markdown files, not DB rows); fake-github has its default seeded PRs + repos.
 */
export async function resetStack(): Promise<void> {
  await Promise.all([
    jsonPost(`${YAAOS_URL}/api/testing/reset`, {}),
    jsonPost(`${FAKE_GITHUB_URL}/__test/reset`, {}),
  ]);
}

/** Seed an active GitHub install on the chosen org (and the matching Claude
 *  Code settings). Use this in specs that aren't testing the install UI —
 *  it bypasses the handshake. The platform GitHub App credentials come from
 *  the test compose's env vars.
 *
 *  `targetOrgSlug`, when set, attaches the install row to the yaaos org with
 *  that slug (seeded earlier via `bootstrap_owner`). Default behavior (no
 *  slug) keeps the legacy single-org stub for unauthenticated specs.
 */
export async function seedGithubInstall(
  opts: { orgLogin?: string; targetOrgSlug?: string } = {},
): Promise<void> {
  await jsonPost(`${YAAOS_URL}/api/testing/seed/github_install`, {
    org_login: opts.orgLogin ?? "acme",
    target_org_slug: opts.targetOrgSlug,
  });
}

/** Seed the `skill_name` for a connected repo so `build_review_invocation`
 *  can resolve a non-null skill handle. Required before dispatching a real
 *  review in e2e specs (otherwise the review step fails with "skill_name
 *  not configured").
 */
export async function seedRepoSkill(opts: {
  orgSlug: string;
  repoExternalId: string;
  skillName: string;
}): Promise<void> {
  await jsonPost(`${YAAOS_URL}/api/testing/seed/repo_skill`, {
    org_slug: opts.orgSlug,
    repo_external_id: opts.repoExternalId,
    skill_name: opts.skillName,
  });
}

/** Insert a single lesson via the testing surface. For specs that need a
 *  pre-existing lesson as a *precondition*, not as the thing under test.
 */
export async function seedLesson(opts: {
  repo_external_id: string;
  title: string;
  body: string;
}): Promise<void> {
  await jsonPost(`${YAAOS_URL}/api/testing/seed/lesson`, opts);
}

/** Build the GitHub webhook payload yaaos's intake/parser will accept. */
export function prPayload(opts: {
  repo: string;
  number: number;
  title: string;
  body?: string;
  action?: "opened" | "synchronize" | "ready_for_review" | "closed" | "reopened";
  headSha?: string;
  baseSha?: string;
  before?: string;
  after?: string;
  draft?: boolean;
}): Record<string, unknown> {
  const headSha = opts.headSha ?? `head-${opts.repo.replace("/", "-")}-${opts.number}`;
  const baseSha = opts.baseSha ?? `base-${opts.repo.replace("/", "-")}-${opts.number}`;
  return {
    action: opts.action ?? "opened",
    before: opts.before,
    after: opts.after ?? headSha,
    pull_request: {
      number: opts.number,
      title: opts.title,
      body: opts.body ?? "",
      draft: opts.draft ?? false,
      merged: false,
      state: "open",
      html_url: `https://github.com/${opts.repo}/pull/${opts.number}`,
      user: { login: "alice", type: "User" },
      head: { ref: "feat", sha: headSha, repo: { fork: false } },
      base: { ref: "main", sha: baseSha },
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    },
    repository: { full_name: opts.repo },
    installation: { id: "fake-install-1" },
  };
}

/** Dispatch a HMAC-signed webhook from fake-github → yaaos. fake-github's
 *  /__test/dispatch_webhook does the signing with the shared test secret.
 *
 *  For `pull_request` events we also seed the PR JSON into fake-github
 *  BEFORE dispatching, so the reviewer's subsequent `fetch_pr` call returns
 *  200 instead of 404. fake-github's default seed only covers `acme/web#1` /
 *  `acme/api#1`; specs use arbitrary PR numbers, so the auto-seed is what
 *  makes them work. Diff content is left untouched — specs that care about
 *  diff content (e.g. the secrets pre-flight) call `seedPRDiff` themselves;
 *  specs that don't care get an empty diff from fake-github, which the stub
 *  coding agent handles fine.
 */
export async function dispatchWebhook(opts: {
  event: "pull_request" | "issue_comment" | "pull_request_review_comment" | "installation";
  payload: Record<string, unknown>;
  deliveryId?: string;
}): Promise<void> {
  if (opts.event === "pull_request") {
    const pr = opts.payload.pull_request as Record<string, unknown> | undefined;
    const repo = opts.payload.repository as { full_name?: string } | undefined;
    if (pr && repo?.full_name) {
      const [owner, repoName] = repo.full_name.split("/");
      await jsonPost(`${FAKE_GITHUB_URL}/__test/seed_pr`, {
        owner,
        repo: repoName,
        number: pr.number,
        pr,
      });
      // Seed a default diff + file list so yaaos's reviewer admission
      // pipeline sees `src/example.ts` in the PR's diff (the stub coding
      // agent emits findings anchored there). Specs that need a custom
      // diff call `seedPRDiff` explicitly after `dispatchWebhook` to
      // overwrite this.
      await jsonPost(`${FAKE_GITHUB_URL}/__test/seed_diff`, {
        owner,
        repo: repoName,
        number: pr.number,
        // `if_unset` lets specs that pre-seed via `seedPRDiff` win.
        if_unset: true,
        diff: [
          "diff --git a/src/example.ts b/src/example.ts",
          "index 0000000..1111111 100644",
          "--- a/src/example.ts",
          "+++ b/src/example.ts",
          "@@ -1,1 +1,2 @@",
          " export {};",
          "+// stub coding-agent finding lands on this line",
          "",
        ].join("\n"),
        files: [
          { filename: "src/example.ts", status: "modified", additions: 1, deletions: 0 },
        ],
      });
    }
  }
  await jsonPost(`${FAKE_GITHUB_URL}/__test/dispatch_webhook`, {
    event: opts.event,
    payload: opts.payload,
    target_url: `${YAAOS_INTERNAL_URL}/api/intake/github`,
    delivery_id: opts.deliveryId ?? `delivery-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
  });
}

/** Seed a PR + its diff inside fake-github so subsequent yaaos API calls
 *  pulling the PR's diff find non-empty content.
 */
export async function seedPRDiff(opts: {
  repo: string;
  number: number;
  diff: string;
  files?: Array<{ filename: string; status: string; additions: number; deletions: number }>;
}): Promise<void> {
  const [owner, repo] = opts.repo.split("/");
  await jsonPost(`${FAKE_GITHUB_URL}/__test/seed_diff`, {
    owner,
    repo,
    number: opts.number,
    diff: opts.diff,
    files:
      opts.files ?? [
        { filename: "src/example.ts", status: "modified", additions: 3, deletions: 1 },
      ],
  });
}

/** Force the next `/compare` call for the given before…after range to return
 *  `status: "diverged"`, simulating a force-push.
 */
export async function seedCompareDiverged(beforeSha: string, afterSha: string): Promise<void> {
  await jsonPost(`${FAKE_GITHUB_URL}/__test/seed_compare_status`, {
    base_to_head: `${beforeSha}...${afterSha}`,
    status: "diverged",
  });
}

/** Fetch comments that yaaos has posted to fake-github (both inline review
 *  comments on PR diffs and non-inline issue-comments on the PR). */
export async function postedComments(): Promise<Array<Record<string, unknown>>> {
  const r = await fetch(`${FAKE_GITHUB_URL}/__test/posted_comments`);
  return (await r.json()) as Array<Record<string, unknown>>;
}

/**
 * Return the HEAD SHA of a fake-github bare repo. Cross-plane e2e specs pass
 * this as `headSha` in `prPayload` so the agent's `git checkout --detach <sha>`
 * resolves against the real bare repo that fake-github serves via git HTTP.
 */
export async function gitHeadSha(owner: string, repo: string): Promise<string> {
  const r = await fetch(`${FAKE_GITHUB_URL}/__test/git_head_sha/${owner}/${repo}`);
  if (!r.ok) {
    throw new Error(`gitHeadSha ${owner}/${repo} → ${r.status}: ${await r.text()}`);
  }
  const body = (await r.json()) as { sha: string; error?: string };
  if (!body.sha) {
    throw new Error(`gitHeadSha ${owner}/${repo}: empty sha (${body.error ?? "unknown"})`);
  }
  return body.sha;
}

/**
 * Return the most-recent workflow run state for the ticket matching `title`
 * on `orgSlug`. Returns `null` when the ticket or runs aren't found yet, or
 * when the most recent run is still in a non-terminal state.
 *
 * Used by failure-path specs to assert the workflow actually completed with
 * a `failed` state (vs never having started at all).
 *
 * Uses `GET /api/tickets/:id/workflow-runs` which is the canonical source of
 * truth for workflow lifecycle state.
 */
export async function ticketJobStatus(
  orgSlug: string,
  title: string,
  request: APIRequestContext,
): Promise<string | null> {
  const listResp = await request.get(`${YAAOS_URL}/api/tickets?q=${encodeURIComponent(title)}`, {
    headers: { "X-Yaaos-Org-Slug": orgSlug },
  });
  if (!listResp.ok()) return null;
  const body = (await listResp.json()) as { items: Array<{ id: string; title: string }> };
  const list = body.items ?? [];
  const ticket = list.find((t) => t.title === title);
  if (!ticket) return null;

  const runsResp = await request.get(`${YAAOS_URL}/api/tickets/${ticket.id}/workflow-runs`, {
    headers: { "X-Yaaos-Org-Slug": orgSlug },
  });
  if (!runsResp.ok()) return null;
  const runs = (await runsResp.json()) as Array<{ state: string }>;
  if (runs.length === 0) return null;
  // Most recent run is last (API returns oldest-first).
  const latestRun = runs[runs.length - 1];
  if (!latestRun) return null;
  // Return `null` while still running; surface the state once terminal.
  const terminalStates = ["done", "failed", "cancelled"];
  if (!terminalStates.includes(latestRun.state)) return null;
  return latestRun.state;
}

/**
 * Log in as a freshly-seeded owner on the given org and land on the dashboard.
 * Resets both stacks, seeds the owner profile, seeds a GitHub install, and
 * completes the OAuth test flow.
 *
 * @param orgSlug defaults to "acme"; override when a test needs a different org.
 */
export async function loginAsOwner(
  page: Page,
  request: APIRequestContext,
  orgSlug = "acme",
): Promise<void> {
  await resetStack();
  await request.post(`${YAAOS_URL}/api/testing/seed/bootstrap_owner`, {
    data: {
      email: "owner@yaaos.test",
      github_id: "1001",
      org_slug: orgSlug,
      display_name: "Owner",
      provider: "test",
    },
  });
  await request.post(`${YAAOS_URL}/api/testing/oauth_test/stage_profile`, {
    data: {
      external_subject: "1001",
      primary_email: "owner@yaaos.test",
      email_verified: true,
      display_name: "Owner",
    },
  });
  await seedGithubInstall({ targetOrgSlug: orgSlug });
  await page.goto(`${YAAOS_URL}/login`);
  await page.getByTestId("login-test").click();
  await page.waitForURL(new RegExp(`/orgs/${orgSlug}/dashboard$`));
}
