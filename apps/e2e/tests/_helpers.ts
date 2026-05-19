/**
 * Shared utilities for e2e specs.
 *
 * Each spec drives its own preconditions in `beforeEach` by composing these
 * helpers — there is no batch-seeded fixture. Reset wipes yaaos's DB and
 * fake-github's in-memory state, then specs call `seedCredentialsAndInstall`
 * if they need the system in a "ready" state.
 *
 * URL envelope:
 *   - `YAAOS_URL`           — yaaos's UI, hit from the browser (default :58080).
 *   - `FAKE_GITHUB_URL`     — fake-github's UI, hit from the test runner.
 *   - `YAAOS_INTERNAL_URL`  — yaaos from inside fake-github's docker network.
 *     Used as `target_url` for webhook dispatch (fake-github → yaaos).
 */

export const YAAOS_URL = process.env.YAAOS_BASE_URL ?? "http://localhost:58080";
export const FAKE_GITHUB_URL = process.env.FAKE_GITHUB_URL ?? "http://localhost:58081";
export const YAAOS_INTERNAL_URL = process.env.YAAOS_INTERNAL_URL ?? "http://yaaos:8080";

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

/** Make yaaos "ready" — credentials + active install — without going through
 *  the manifest flow. Use this in specs that aren't testing the setup UI.
 */
export async function seedCredentialsAndInstall(orgLogin = "acme"): Promise<void> {
  await jsonPost(`${YAAOS_URL}/api/testing/seed/credentials_and_install`, {
    org_login: orgLogin,
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
    target_url: `${YAAOS_INTERNAL_URL}/api/github/webhook`,
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
