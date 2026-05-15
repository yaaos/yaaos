# yaaof — Design Brief

> Briefing for a UI design pass. Covers what the product is at the long-horizon level, the M01 surfaces that need designing now, and the vocabulary + constraints the design must respect.
> Audience: a design tool (Claude Design) producing high-fidelity mockups that will be implemented in React + Tailwind + shadcn/ui.

---

## 1. What yaaof is

**yaaof turns Linear/Jira tickets, Slack threads, and operational alerts into reviewed, tested, ready-to-merge pull requests — a team-scale agent orchestration service for engineering teams of 2–100.**

The audience is teams stuck between two bad options. Today's coding agents (Claude Code, Codex, Aider, Composio's orchestrator) are individual-developer tools — every engineer runs them on their own laptop, with no shared visibility, no shared budget, no consistent review standards. The team-scale alternatives are heavyweight enterprise platforms. The team in the middle wants to dispatch a coding agent from a Linear ticket, watch it work in a place everyone can see, have review agents gate its output, and pull a human in only when the agents are stuck.

yaaof is the self-hosted service that does that:

- A team member triggers work from where they already are — a Linear ticket, a Slack thread, an ops alert, a PR opened on GitHub.
- A **coding agent** opens a worktree, writes code, runs tests in an ephemeral environment yaaof provisions for it.
- **Review agents** (architecture, security, style, plus anything custom the team defines) gate the resulting PR.
- Review feedback flows back to the coding agent until tests pass and reviews clear.
- The whole team sees one shared dashboard, gets pinged in Slack when attention is needed, approves the merge.
- Agents **remember feedback** so the team's preferences accumulate as institutional memory.
- **Every step is auditable** — prompt, tool call, file change, test result, review verdict — in a human-readable timeline on the ticket.

Core principles:

- **Opinionated defaults, configurable everywhere.** Get going in under an hour with stock settings. Every default is replaceable without forking.
- **One shared view, not many private ones.** Most actions are visible to the whole team by default. Filter, don't partition.
- **Humans set policy; agents execute it.** Auto-merge vs. wait-for-approval is configuration, not hard-coded.
- **Memory is institutional.** What one agent learns, the team's future work benefits from.
- **Every action is auditable.** If you can't answer "why did the agent do that," we built it wrong.
- **Composable agents, not a frozen pipeline.** Adding a new review agent is a config change, not a code change.
- **Self-hosted.** The team owns its data, agents, model keys, and budget.

---

## 2. Users & personas

**Two personas with overlapping but distinct goals.**

### Admin / operator
- Installs yaaof, points it at a GitHub App, sets the model API key, adds repos to the allowlist.
- Edits review-agent prompts.
- Curates per-repo lessons ("remember this").
- Watches the dashboard for failures, agent errors, cost spikes.
- Reads audit logs when investigating "why did the agent do that?"
- In M01, all yaaof users are effectively admins (no auth yet). The UI should still respect the conceptual distinction so the future auth retrofit is mechanical.

### Team member (engineer)
- Opens a PR; expects three review comments to appear within a few minutes.
- Reads agent feedback on their PR comments directly in GitHub.
- Occasionally clicks into yaaof to see the full audit log for a tricky review.
- Occasionally writes a lesson ("don't suggest mocks in this repo") from the yaaof UI when the team makes a recurring correction.
- Re-runs an agent via UI button when they pushed a fix.

**Implications for design:** the dashboard and the ticket-detail page get the most traffic from engineers. The settings/repos/prompts pages get visited mostly by admins, occasionally. Memory is mixed — engineers write lessons, admins curate them.

---

## 3. Information architecture

**Top-level surfaces** (left-nav order suggested):

1. **Dashboard** — landing page. At-a-glance state of the system. Onboarding banners when something isn't configured. Recent activity. Service-level metrics for admins.
2. **Tickets** — list view (filtered, sorted, infinite-scroll) and detail view per ticket.
3. **Memory** — per-repo lessons: list, edit, delete.
4. **Prompts** — agent prompt editor: one text area per built-in review agent (architecture / security / style). Reset-to-default.
5. **Repos** — allowlist management. Add by VCS identifier, remove, see per-repo status.
6. **Settings** — GitHub App install state, model API key entry, plugin health.

**Nested within Tickets / detail:**
- Header: ticket title, status badge, linked PR with quick "open in GitHub" link.
- Tabs:
  - **Agents** — three agents (or however many configured), each as a card showing status, verdict, timing, cost, latest comment preview.
  - **Audit log** — full timeline of everything yaaof did on this ticket: prompt sent, review posted, lesson read, error, etc. Filterable by kind and entity.

**No global search in M01.** Each list page has its own filters.

**No user menu / account / org switcher.** Single org, no auth.

---

## 4. Domain entities & value objects

These are the nouns the UI surfaces. Every UI element should map to one of these.

### Entities (have identity, mutable state)

| Entity | What it is | Key fields the UI shows |
|---|---|---|
| **Ticket** | yaaof's unit of work. M01: one ticket per GitHub PR. M02+: also Linear/Jira/Slack/ops alerts. | title, description, status, source kind, linked PR, repo, created/updated timestamps, actor (who triggered) |
| **Pull Request** | A GitHub PR yaaof is tracking. Mirrors VCS state. | number, title, description, author, head/base branch, state (open/closed/merged), is_draft, html_url |
| **Repo** | A repository on the allowlist. | display name, plugin (github), external id, language hint, status |
| **Review Job** | One review agent's attempt to review one PR. | which agent, status, verdict, timing, token usage, cost, current step (when running), heartbeat freshness |
| **Reviewer Agent** | A configured review agent (M01: 3 built-in — architecture, security, style). | name, prompt text, coding-agent plugin id (which CLI runs it), is_built_in flag |
| **Lesson** | A per-repo piece of human-supplied guidance to apply to future reviews. | title, body (≤1000 chars), source PR link, created/updated timestamps |
| **Audit Entry** | One row in the per-ticket timeline. Immutable. | kind (e.g., `review_job.posted`), payload (structured), actor, timestamp |
| **Workspace** | A provisioned environment where an agent ran. M01: tempdir + git clone. Admin-visible mostly. | id, state, provider, age, last heartbeat |

### Value objects (immutable, equal by attributes)

- **Actor** — who did a thing. Kind is one of `github_user` (with login), `agent` (with agent name), `system`.
- **Finding** — a single piece of agent feedback inside a review: file/line, severity, title, body.
- **Review** — bundle of findings + verdict posted on a PR by one agent.

### State vocabularies (badges, chips, color coding)

These need consistent visual treatment across the app. Design should establish a palette for each.

| State group | Values | UI treatment |
|---|---|---|
| **Ticket status** | `open` / `in_review` / `complete` / `abandoned` | Badge in ticket card + header |
| **PR state** | `open` / `closed` / `merged` | Small chip beside the PR link |
| **Review verdict** | `APPROVED` / `CHANGES_REQUESTED` / `COMMENT` | Color-coded chip per agent (green / red / neutral) |
| **Review job status** | `queued` / `running` / `posted` / `failed` / `skipped` / `cancelled` | Animated state in the agent card (running pulses, etc.) |
| **Skip reason** | `draft` / `fork` / `bot_author` / `trivial_diff` / `too_large` / `crashed` / `secrets_detected` | Subtle text under the agent card |
| **Workspace state** | `creating` / `active` / `expired` / `destroying` / `destroyed` / `destroy_failed` | Admin view only; `destroy_failed` is loud |
| **Finding severity** | `must-fix` / `nit` / `suggestion` / `info` | Inline icon + color in finding lists |
| **Actor kind** | `github_user` / `agent` / `system` | Avatar style differs per kind (GH avatar / agent emblem / system glyph) |

---

## 5. Key user flows

### Flow A — First-run onboarding (admin)

1. Admin lands on **Dashboard**. Empty state shows three banners:
   - "Install the GitHub App" (with link to GitHub install flow)
   - "Add your model API key" (link to Settings)
   - "Add a repo" (link to Repos)
2. As each is completed, the banner is replaced with a green check.
3. When all three are green, the dashboard transitions to its populated state.

**Moments that matter:** the empty state should feel like a checklist, not a wall. Each banner is a clear next action.

### Flow B — A PR gets reviewed (engineer)

1. Engineer opens a PR on GitHub.
2. Within seconds, a **new ticket card appears** at the top of the Tickets list (SSE-driven; no refresh).
3. Each of the three review agents progresses live in the **Agents** tab: `queued → running → posted`. The "current step" text updates (assembling prompt / invoking agent / posting review). Heartbeat freshness shows the job hasn't stalled.
4. As each agent posts, three review comments appear on the GitHub PR (engineer sees these in GitHub, not yaaof). The yaaof UI shows the same content under the Agents tab.
5. Engineer pushes a fix → ticket card shows the agents going back to `queued` then re-running.

**Moments that matter:**
- The live progression of three agent cards is **the** core spectacle of yaaof. It should feel kinetic.
- Cost + token totals appear unobtrusively in the agent card footer.

### Flow C — Reading the audit log (anyone)

1. Engineer (or admin) opens a ticket.
2. Switches to **Audit log** tab.
3. Sees a reverse-chronological timeline: scheduling, prompt-sent (with hash), review posted (with verdict + cost), failures, replies, lessons read.
4. Filters by kind: e.g., show only `review_job.failed` to debug a crash.
5. Expands an entry to see its full Pydantic payload (formatted as code).

**Moments that matter:** the audit log is yaaof's most distinctive feature. It should look authoritative — like reading a flight recorder, not a tail of stdout. Dense, monospaced for technical fields, expandable rows.

### Flow D — Writing a lesson (engineer / admin)

1. After a recurring agent miss, user opens **Memory**.
2. Picks the repo from a tab/segmented control at the top.
3. Sees the existing lessons list, newest first.
4. Clicks "New lesson" → modal/sheet with two fields: title, body (1000-char limit shown as a character counter).
5. Saves → toast confirms; the lesson appears in the list immediately.
6. The next review on that repo includes the lesson in the agent prompt (the user can verify this in the audit log's `review_job.prompt_sent` entry, where the `lessons_count` field reflects the new total — but that's deep audit, not main flow).

**Moments that matter:** writing a lesson should feel weightless. The character counter should feel like Twitter, not like a form. Editing and deleting should be one-click from the list, with undo via toast.

### Flow E — Editing an agent's prompt (admin)

1. Admin opens **Prompts**.
2. Sees three tabs: Architecture / Security / Style.
3. Each tab has a single big text area with the current prompt.
4. Edits, hits Save. Validation: non-empty.
5. Toast confirms.
6. "Reset to default" button restores the built-in default prompt (with a confirm modal).
7. The next review on the next PR uses the new prompt; previous in-flight reviews use the prompt snapshotted when they started (audit log shows the prompt hash captured at job start).

**Moments that matter:** the prompt editor should feel like a serious tool, not a tweet box. Monospaced, line-numbered, wide. Reset-to-default is a deliberate action (confirm modal).

### Flow F — Re-running a review (engineer)

1. Engineer opens a ticket.
2. Header has a single **Re-review** button.
3. Click → confirm → all three agents go back to `queued`. The Agents tab reflects this immediately.

**No granular per-agent rerun in M01.** Either all three rerun, or none. Per-agent rerun is also possible via a GitHub comment command (`@yaaof-architecture rereview`), but that's a GitHub flow, not yaaof UI.

### Flow G — Managing the repo allowlist (admin)

1. Admin opens **Repos**.
2. Sees the list of allowlisted repos with status (active / install-missing / unreachable).
3. "Add repo" → input the repo identifier (e.g., `owner/name`) → yaaof verifies the GitHub App has access → adds it.
4. Removing a repo cancels any in-flight reviews and stops yaaof acting on it.

**Moments that matter:** repo-status awareness. A repo that yaaof can't access should be clearly broken (red dot + "reconnect" affordance), not silently dead.

---

## 6. Real-time / live behaviors

yaaof is **live by default**. Most lists and cards update via SSE without the user refreshing.

- **Ticket list:** new tickets appear at the top with a brief highlight; existing tickets re-render in place when their status changes.
- **Ticket detail / Agents tab:** the three (or N) agent cards animate through their state machine: queued → running (with progress text) → posted (with verdict). Heartbeat staleness shows up as a warning.
- **Audit log:** new entries appear at the top of the timeline as agents do work.
- **Dashboard:** the activity ticker and metrics tiles update live.

**Skeleton states** while initial data loads. **Loading dots inline** when an agent is actively working. **Toasts (sonner)** for human-initiated actions (lesson saved, prompt updated, repo added).

**Connection-lost state:** SSE drops → top-of-page banner "reconnecting…" → on reconnect, refetch.

---

## 7. Empty states & onboarding

Empty states are first-impressions; they get the most thought, not the least.

| Surface | Empty state | Suggested treatment |
|---|---|---|
| **Dashboard, fresh install** | Three big checklist banners (install App, set API key, add repo). | Cards, each with the action button and a one-line explanation. As completed, fade to a small green-check row. |
| **Dashboard, configured but no activity** | "yaaof is ready. Open a PR on `<repo>` to see your first review." | A single hero card with a copyable example. |
| **Tickets list, empty** | "No tickets yet. yaaof creates a ticket each time a PR is opened on an allowlisted repo." | Quiet placeholder, link to Repos. |
| **Tickets list, filtered to nothing** | "No tickets match these filters." | Inline near the filter chips, with "Clear filters" button. |
| **Memory, no lessons in repo** | "No lessons for `<repo>` yet. Lessons let you teach yaaof your team's preferences." | Quiet placeholder with "+ New lesson" CTA. |
| **Repos, none allowlisted** | "Add the first repo to start reviewing." | Hero card with the add form right there. |
| **Audit log, brand-new ticket** | (Very rare — there's always at least a `review_job.scheduled` entry.) | Small spinner: "Waiting for activity." |

---

## 8. Visual tone

**Dense, ops-tool feel.** Reference targets: Linear (information density, clean typography), Datadog (live metrics, kinetic data), GitHub's PR page (familiar mental model for the audience).

- Lots of information per screen; small text is fine; tight spacing is preferred.
- Type hierarchy is restrained — most text is one or two sizes; weight + color carry the hierarchy.
- Color is functional: state vocabularies (verdict, severity, status) carry color; chrome is monochrome.
- Tables are dense and useful, not card-grids pretending to be tables.
- Live elements (animations, transitions) are subtle but present — the live progression of agents is meant to feel kinetic, not silent.

**Light + dark mode from day one.** OS-preference auto-detect with a manual toggle in the user menu / a corner of the layout.

**Desktop-only.** Single-org self-hosted internal tool; users live at their workstation. No mobile support needed in M01.

**Accent color: let design decide.** No prior commitment; propose a palette that fits the ops-tool feel.

---

## 9. Tech stack constraints

Mockups should be implementable in this stack. Lean on primitives that exist.

- **Framework:** React 18+ with Vite, TypeScript strict mode.
- **Routing:** TanStack Router (typed routes, code-splitting per route).
- **Server state:** TanStack Query (queries + mutations + SSE-driven cache invalidation).
- **Client state:** Zustand for cross-route state (rare; most state is server state or URL state).
- **UI library:** **shadcn/ui** — copy-paste component library on top of Radix primitives. Use its component vocabulary: Card, Tabs, Badge, Sheet, Dialog, Form (react-hook-form integration), Command palette, Combobox, Table, Skeleton, Tooltip, Popover, Toast, Sonner.
- **Styling:** **Tailwind** (utility-first). No CSS modules, no styled-components, no Emotion. shadcn components are styled via Tailwind class composition.
- **Icons:** **lucide-react** (single icon set; consistent stroke).
- **Toasts:** **sonner** (also available via shadcn).
- **Forms:** **react-hook-form** with **zod** for schemas. shadcn has a Form wrapper that integrates the two.
- **Dates:** **date-fns** for formatting (relative time, absolute time).
- **Charts (if any):** TBD — `recharts` is the likely choice if metrics tiles show sparklines or line charts.
- **Code blocks (diffs, prompts, JSON payloads):** syntax-highlighting library TBD; mockups can show the rendered result.
- **Real-time:** SSE via native `EventSource`, wrapped in a custom `useEventStream` hook.
- **API:** typed client generated from the backend's OpenAPI schema (`openapi-typescript` + `openapi-fetch`).

**No auth in M01.** No login screen, no user menu, no org switcher, no permissions UI.

**Frontend is dumb.** No business logic in the SPA — it renders data and dispatches actions. Validations exist for UX immediacy but the backend is authoritative.

---

## 10. Out of scope (do not design)

These exist in the long-horizon vision but are explicitly not part of the M01 design pass:

- **Login / sign-up / SSO / user management.** No auth in M01.
- **Multi-org / org switcher.** Single org.
- **Coding agent UI.** M01 is review-only; coding agents (the ones that *write* code) come later.
- **Linear / Jira / Slack intake UI.** M01 intake is GitHub PRs only.
- **Ephemeral test environment provisioning UI.** Comes with coding agents.
- **Merge-gating / branch-protection UI.** Reviews are advisory in M01.
- **Budget UI / cost caps / per-user budget attribution.** Cost is tracked and displayed but not capped.
- **Custom user-defined review agents.** Three built-in agents only; prompt-editable, but the set is fixed.
- **Aggregated cross-agent verdict.** Each agent comments independently; no "approved by 2 of 3" roll-up.
- **Notification routing (Slack pings, email).** Out of M01 entirely.
- **Mobile / tablet layouts.** Desktop only.
- **Marketing-site chrome.** This is an internal tool.

---

## Appendix — Vocabulary cheatsheet

For consistent UI copy:

- "yaaof" lowercase (project name; acronym-ish).
- "Review agent" not "reviewer" (reviewer is the backend module).
- "Lesson" not "memory item" (memory is the feature; lessons are the things in it).
- "Re-review" (hyphenated) not "rerun review."
- "Ticket" not "task" or "job" — yaaof's unit of work.
- "Review job" — one agent's attempt at reviewing one PR. (Plural: review jobs.)
- "Audit log" not "activity feed" or "event log."
- "Verdict" — what one agent concluded (APPROVED / CHANGES_REQUESTED / COMMENT).
- "Allowlist" not "whitelist."
- "GitHub App" not "GitHub bot" or "GitHub integration."
