# M01 — Code Review Loop (requirements)

> See [README.md](README.md) for the full milestone index. Companion docs: [architecture.md](architecture.md), [modularity.md](modularity.md), [backend.md](backend.md), [domain-model.md](domain-model.md), [data-model.md](data-model.md), [frontend.md](frontend.md), [patterns.md](patterns.md).

**Goal:** Three specialist review agents (architecture, security, style) automatically review every pull request opened on a configured repo, accept human feedback, and remember per-repo lessons across PRs.

**Status:** planned
**Target:** no date

## Why

yaaof's value proposition is automated code review by configurable specialist agents. M01 proves that loop end-to-end on a single agent class (review) before adding the coding agent, intake adapters, or tenancy. It also forces us to confront the cross-cutting commitments — per-ticket audit log, service observability, per-repo memory, configurability — early enough that they aren't retrofits.

## Tickets and PRs

yaaof's unit of work is a **ticket**. A ticket represents one piece of work flowing through the system from intake through completion. Every yaaof-tracked operation — review attempts, audit-log entries, agent invocations — hangs off a ticket.

In M01, every ticket originates from a GitHub PR. When the `github` plugin receives a webhook for an allowlisted, reviewable PR, yaaof upserts the PR's metadata into its internal `pull_requests` table (the VCS mirror) AND creates or updates a corresponding ticket. The ticket carries:

- A pointer to its source (the linked PR + repo).
- Title and description (in M01 these mirror the PR's title and body).
- Status: `in_review` while the PR is open and being acted on; `complete` when the PR closes/merges; `abandoned` if it's removed from the allowlist or never reaches review.
- The audit-log timeline of every agent and human action.

**The UI is ticket-centric.** The list view shows tickets, not PRs. The detail view shows a ticket with its linked PR information surfaced prominently. URLs look like `/tickets/<uuid>`. PRs are referenced from tickets, but the ticket is the navigation primitive.

This matters because in later milestones tickets originate from Linear, Jira, Slack, or operational alerts — and for those sources the coding agent attaches a PR mid-lifecycle. M01 establishes the data model and UI shape so M02+ adds new ticket sources without restructuring the world.

## In scope

### Review agents
- Three specialist agents: **architecture**, **security**, **style**.
- Each runs independently. All three post under a **single yaaof GitHub App identity**; agent attribution is via a comment-body prefix (`[architecture]` / `[security]` / `[style]`). See Decisions § GitHub integration.
- Each agent's prompt is editable via the yaaof UI.
- **Each agent is an invocation of the Claude Code CLI** running inside a workspace where the PR's repo is checked out at the head SHA. Claude Code does its own LLM calls, tool use, and code exploration. yaaof's job is to assemble the prompt, invoke the CLI, parse the output (structured JSON findings), and post the review.
- **The yaaof Python process never makes an LLM API call in M01.** All LLM work happens inside the Claude Code CLI subprocess.
- M02+ may add other coding-agent CLIs (Codex, Aider, etc.) as plugins; agents can be configured to use different CLIs.

### Triggers
- New PR opened on an allowlisted repo → all three agents review.
- New commit pushed to an existing PR → all three agents re-review.
- Human replies to an agent's GitHub comment → that specific agent responds and may revise its take.
- Explicit "re-review" command (from yaaof UI or a PR comment) → all three rerun.

### Repo configuration
- Admin-managed allowlist of repos yaaof is permitted to act on.
- Adding/removing repos done from the yaaof UI.

### PR metadata sync

- On **every webhook** about a tracked PR (open, sync, edit, comment, reaction, close, etc.), yaaof re-fetches the PR's current metadata and updates both the `pull_requests` row (shas, draft/ready state, html_url, etc.) and the linked ticket's title + description.
- This keeps yaaof's view fresh without needing a separate scheduler.

### Per-repo memory
- Humans leave long-term feedback via the yaaof UI ("remember: don't suggest mocks in this repo").
- Lessons are persisted **per repo**.
- Each future review on that repo includes its lessons in the agent prompt.
- UI to view, edit, and delete lessons per repo.

### Per-ticket audit log
- Human-readable timeline of every action on a ticket: prompt sent, model invoked, tool call made, GitHub API call, comment posted, memory written, memory read, human reply received, re-review triggered.
- Linkable and shareable. Visible in the yaaof UI per ticket.

### Observability
- Basic service metrics: PRs reviewed, time-to-first-comment, cost per review, agent failure rate, re-review count.
- Structured logs and distributed traces for background jobs.

### UI
- Ticket list view (all tickets yaaof has touched) with per-agent status.
- Ticket detail view: linked PR information, audit log, agent verdicts, links to the GitHub PR, **"Re-review" button that reruns all three agents**. (No separate "Memory used" tab — current lessons for the repo are assumed-applied.)
- Repo allowlist management.
- Agent prompt editor (three prompts, one per agent).
- Per-repo memory management (list, edit, delete lessons).

## Out of scope (explicit)

- Coding agent (writing or modifying code).
- Intake from Linear, Jira, Slack, ops alerts. **PRs only.**
- Ephemeral test environment provisioning.
- Merge gating or blocking PR status checks.
- Authentication, users, RBAC. (See [architecture.md § Security](architecture.md#security) for the POC baseline yaaof still maintains without auth — encryption at rest, HMAC webhook verification, parametrized SQL, no shell injection paths, secrets never logged.)
- Multi-org / multi-tenant.
- Budget enforcement (cost is tracked in metrics; no hard caps).
- Aggregated verdict across agents (each comments independently — no roll-up "approve/block").

## Decisions

Decisions made up-front so implementation can run autonomously. Each is a requirement, not a suggestion.

### GitHub integration
- **Single GitHub App.** Comments are prefixed `[architecture]` / `[security]` / `[style]` to distinguish agents. (Not three separate App installs.)
- **Agents post full PR reviews** (not bare comments). Each review has a state.
- **Verdict rule:** `APPROVED` when the agent has no findings; `CHANGES_REQUESTED` when any finding is tagged `must-fix`; otherwise `COMMENT`.
- **Finding schema** (each agent emits a list of these as its structured output):
  - `file` — path relative to repo root.
  - `line_start`, `line_end` — line numbers in the new file; both required for line comments; omit both for a review-body finding.
  - `severity` — one of `must-fix` | `nit` | `suggestion` | `info`. Only `must-fix` flips the verdict to `CHANGES_REQUESTED`.
  - `title` — short summary (≤120 chars).
  - `body` — full explanation, markdown allowed.

### Which PRs trigger a review
- **Skip draft PRs.** Trigger only when a PR is marked ready-for-review (or opened non-draft). Converting an existing draft to ready-for-review is treated identically to opening a non-draft PR — it starts the debounce window and triggers a full review.
- **Skip fork PRs.**
- **Skip bot-authored PRs** (identified by author type=Bot).
- **Any target branch** is in scope (not only the default branch).
- **First install on a repo:** forward-only. Existing open PRs are not backfilled.

### Commit-level triggers
- **Debounce window: 30 seconds** after the last commit on a PR before triggering a review.
- **Force push:** triggers a re-review. On GitHub, prior comments on lines that no longer exist are marked outdated automatically by GitHub itself — yaaof does not call an explicit outdate-marking API. The `vcs.mark_comments_outdated` Protocol method exists for future plugins that lack equivalent automatic behavior; for `plugins/github` it is a no-op.
- **Trivial-diff skip:** if a commit changes only files on the skip list (see below), no review is triggered.
- **PR reopen** alone does not trigger; the next commit will.
- **Mid-review commit:** the in-flight review is cancelled; the debounce timer restarts on the new commit. The discarded work is recorded in the audit log.

### Re-review diff scope
- **Full PR diff every time.** Re-reviews do not use incremental diffs.
- **Prior agent comments are included as context** in the prompt for re-reviews.

### Job-start snapshot
- At the moment a ReviewJob starts, yaaof **snapshots** the agent prompt, the agent's `coding_agent_plugin_id` + `agent_config`, the per-repo lessons, the PR diff, the PR metadata, the prior agent comments, AND the workspace's checkout sha. The snapshot is recorded in the audit log (`review_job.prompt_sent` entry).
- **Mid-flight edits do not affect an in-flight job.** If an admin edits the agent prompt or a lesson while a job is running, the in-flight job uses the snapshot taken at start time; the next job picks up the change.
- **The CLI agent's internal work (LLM calls, file reads, etc.) is NOT yaaof's audit trail.** The CLI may emit its own logs; yaaof captures what it can from the CLI's structured output (token usage, cost, findings) but does not attempt to mirror every internal LLM call or tool dispatch.

### Cross-agent visibility
- Each agent **sees the other two yaaof agents' comments** on the PR. (Not human comments.)

### Human reply triggers
A re-reply is triggered when a human:
- Replies in the inline-comment thread under an agent's line comment.
- Posts a top-level PR comment containing the parsed token `@yaaof-architecture` (or `-security`, `-style`). (See note below — these are body-parsed tokens, not GitHub user mentions.)

A re-reply is **not** triggered by:
- Top-level PR comments without an @-mention.
- Emoji reactions. Reactions (👍 / 👎) are captured as signal in the audit log and may be used by the memory system later, but do not produce a reply.

**Agent reply mechanic:** the agent posts a new follow-up comment in the same thread. It never edits its original comment.

**Re-review command syntax (in PR comments):**
- `@yaaof rereview` → all three agents re-run.
- `@yaaof-architecture rereview` (or `-security`, `-style`) → that agent re-runs alone.

The `@yaaof...` strings are **parsed from comment bodies, not GitHub user mentions**. No GitHub user named `yaaof` exists (yaaof has a single App identity, not a user). The leading `@` is a syntactic marker for the command parser, nothing else. Implementers: wire this off the comment-body regex in the `comment_created` webhook handler, not off GitHub's mention-webhook event.

### Per-repo memory (lessons)
- **Retrieval:** all lessons for the repo are included in every review prompt.
- **Scope:** lessons are shared across all three agents on a repo (not per-agent).
- **Format:** each lesson has a **title**, **body**, and **source** (link to the PR comment that originated it).
- **Size cap:** 1000 characters hard cap on the body.
- **Input channel:** UI only. PR-comment syntax for adding lessons is **not** supported in M01.

### Failure handling
- **Model API failure** (timeout / 5xx / rate limit): retry with exponential backoff (3 attempts over ~30s). On final failure, mark that agent's review as `failed` in the audit log and post nothing for that agent. Other agents are unaffected.
- **Missed webhooks** (yaaof was down): on startup, poll GitHub for PR events since the last seen event per repo and process them.
- **Prompt-injection defense:** pre-prompt the model with explicit instructions to treat all PR content (title, body, diff, comments) as untrusted input and ignore embedded directives. Logged in the audit trail.
- **Secrets detected in diff:** refuse to review. Post a single PR comment asking the human to remove the secret. Audit log records the detection (without the secret itself).

### Edge-case lifecycle
- **PR closed or merged mid-review:** in-flight reviews are cancelled; nothing is posted; audit log captures the cancellation.
- **Repo removed from allowlist mid-review:** in-flight reviews are cancelled immediately; nothing is posted.
- **Diff size > 5000 changed lines:** skip the review. Post a single PR comment explaining the PR is too large. Status = `skipped` with reason `too_large`.

### Performance & status
- **SLO:** P95 time from triggering event to first agent comment posted is under **10 minutes**.
- **Per-agent status state machine:** `queued` → `running` → one of `posted` / `failed` / `skipped` / `cancelled`. The `skipped` status carries a reason (`draft` / `fork` / `bot_author` / `trivial_diff` / `too_large`).

### Audit log
- **Retention target: 90 days** — but **pruning implementation is deferred** for POC. The table grows unbounded in M01 until storage/query perf becomes a real concern; then a periodic prune job is added.
- **Visible in the yaaof UI** per ticket. Captures every agent action: prompt sent, model invocation, tool call, GitHub API call, comment posted, memory read, memory written, retries, cancellations, skip reasons.
- **Every entry records an `Actor`** (kind + login if human + agent_id if agent) and a timestamp. Human-originated entries (replies, reactions, re-review commands) link to the originating GitHub comment / reaction.

### Observability
- **OpenTelemetry traces** are emitted for all agent and webhook flows. The trace backend / sink is an architecture decision, not a requirement.
- **Service metrics** required: PRs reviewed, time-to-first-comment, cost per review (token usage × price), agent failure rate, re-review count.

### Files excluded from the diff before agents see it
- Lockfiles (`package-lock.json`, `yarn.lock`, `Cargo.lock`, `poetry.lock`, `Pipfile.lock`, `Gemfile.lock`, `go.sum`).
- Vendored dependency directories (`node_modules/`, `vendor/`, `third_party/`).
- Binary files and images (by file extension and Git binary detection).
- Files matching `linguist-generated` in the repo's `.gitattributes`.
- Common generated-code conventions: `*.pb.go`, `*_generated.*`, `*.gen.*`, `dist/`, `build/`, `out/`.

### Language awareness
- yaaof tracks a primary language per repo (`repos.language_hint`), computed **once** on the first successful review for that repo (sampled by file-extension share across the changed files of that PR) and reused thereafter. It is injected into the agent prompt (e.g., "This repo is primarily Python.").
- Per-PR re-detection is **not** done — keeps the model simple and avoids contradictions between the repo's identity and individual PR samples. If the repo's mix changes over time, an admin can manually clear `language_hint` from the UI to trigger re-detection on the next review.

### UI: ticket list

- **Filters:** by repo (multi-select), by ticket author (in M01 = PR author), by date range.
- **Sort:** most recent activity first.
- **Pagination:** infinite scroll.
- **Live updates:** rows update in real time via Server-Sent Events as agents progress through statuses.

### UI: ticket detail

- **Layout:** header with ticket title + linked PR summary + **single "Re-review" button** (reruns all three agents on the underlying PR; **hidden when the linked PR is closed or merged** — the ticket detail still shows the audit log and past results, just no re-trigger affordance) + tabbed view of `Agents` | `Audit log`.
- **Review tab:** each of the three agents' status, verdict, findings, links to the GitHub review.
- **Audit log tab:** chronological timeline of every action on this ticket.

### Onboarding

- **Empty dashboard with banner prompts** for missing prerequisites. No guided wizard. The admin lands on the main UI; banners surface "install GitHub App," "set model API key," "add a repo," etc., until each is complete.
- **Model provider API key** is entered via the yaaof UI and stored **encrypted** in the database. No env-var or config-file path in M01.

### Agent prompts

- **No variable placeholders.** The editable prompt is the agent's *instruction* only. yaaof always appends context (diff, lessons, language, prior agent comments, PR title/body) in a **fixed format** after the instruction. Admins cannot reorder or omit context.
- **Empty prompts are rejected at save time** with a validation error.
- **Reset-to-default** button is always visible on the prompt editor.

### Trigger coalescing

- **Per-(PR, agent) job queue.** Each (PR, agent) pair has at most one active review job at a time. A new trigger that affects all three agents (commit, "re-review all" command/button) cancels all three in-flight jobs and restarts the debounce window for each. A trigger that affects one agent only (a human reply to that agent's comment, `@yaaof-architecture rereview`) cancels just that agent's in-flight job. This subsumes the "mid-review commit" decision above.

### CODEOWNERS, CI, per-repo overrides — explicit no-ops for M01

- **CODEOWNERS:** ignored.
- **CI / GitHub Actions status:** ignored. Agents do not wait for CI and do not see its status.
- **Per-repo setting overrides:** none. All settings (debounce window, diff cap, agent prompts, etc.) are global. Per-repo overrides are deferred to a later milestone.

## Done means

- A user can configure two repos via the yaaof UI, open a PR in either, and within reasonable time see three review comments posted under the single yaaof GitHub App identity (prefixed `[architecture]` / `[security]` / `[style]`). A corresponding **ticket** appears in the UI list, with status `in_review`.
- Opening the ticket detail page shows the linked PR, agent statuses, audit-log timeline, and the lessons injected into the prompt.
- Replying to an agent's PR comment on GitHub causes that agent to respond.
- Pushing a new commit causes all three agents to re-review (after the debounce window).
- Clicking the **"Re-review" button** on the ticket detail page reruns all three agents on the linked PR.
- Clicking "remember this lesson" in the UI persists a per-repo lesson; the next review on that repo demonstrably includes it in the prompt (verifiable in the prompt-hash recorded in the audit log).
- Every action above is visible in the per-ticket audit log timeline.
- Basic metrics (tickets reviewed, cost per review, failure rate) are visible somewhere queryable.
- **Per-app `bin/ci` scripts exist and pass on a fresh clone.** Each app owns its own check stack:
  - `apps/backend/bin/ci` — Ruff (lint + format check, incl. TID251 banned-api enforcing the no-`@patch` rule), tach check via `bin/sync_modules --check`, `bin/check_table_access`, pytest (unit + integration).
  - `apps/web/bin/ci` — Biome (lint + format check), TypeScript type-check, Vitest, OpenAPI codegen-drift check.
  - `apps/e2e/bin/ci` — brings up `docker/docker-compose.test.yml` (Postgres pre-seeded + fake-github + yaaof), runs Playwright against it, tears down. No external credentials required; fully self-contained.
  - **No top-level `bin/`.** Per-app tooling (incl. `apps/backend/bin/sync_modules`, `apps/backend/bin/check_table_access`) lives in each app's `bin/` directory. There is no top-level `bin/ci` either — CI runs each per-app script.
