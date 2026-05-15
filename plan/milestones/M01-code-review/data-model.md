# M01 — Data Model (planned)

> Cross-cutting picture of every Postgres table in M01, who owns each, and how they relate.
> Each module's [internals/](internals/) deep-dive owns the column-level detail; this doc owns the **inventory and relationships**.
> Data model is **not** its own module — tables are owned by the modules listed in [backend.md](backend.md). This is a documentation aggregate, not a module boundary.

## Conventions

- **Every ID is a UUID** (yaaof-generated). Plugin-side identifiers are stored as separate `external_id: str` columns.
- **Every timestamp is timezone-aware UTC** (`timestamptz`).
- **Sensitive columns are prefixed `encrypted_`** (e.g., `encrypted_api_key`). Encrypted at rest with the boot-time encryption key.
- **`org_id` is on every table** (even though M01 is single-org). Forward-compat for multi-tenant; scoped by every query.
- **No cascading deletes anywhere.** Deletes are soft (`status` column flips) or audit-trailed. Audit log survives entity deletion.
- **Foreign keys are real DB FKs** unless explicitly marked "loose ref" (audit_entries entity references are loose).

## Relationships

```
                       ┌────────┐
                       │  repos │
                       └───┬────┘
              ┌────────────┼──────────────────┐
              ▼            ▼                  ▼
       ┌─────────┐  ┌────────────┐     ┌──────────┐
       │ tickets │◀ │pull_requests│    │ lessons  │
       └────┬────┘  └──────┬─────┘     └──────────┘
            │   1-1 (M01)  │
            │              ▼
            │       ┌──────────────┐    ┌────────┐
            │       │ review_jobs  │───▶│reviewer_agents│
            │       └──────────────┘    └────────┘
            │
            ▼
     (audit_entries loosely reference any entity by kind+id)
```

Plugin tables: `github_app_installations`, `github_settings`, `github_webhook_events`, `claude_code_settings` stand alone (not FK'd to domain tables). `github_poller_state` references `repos` via FK. `workspaces` (owned by `core/workspace`) stands alone — the `spec` JSONB references a repo by `(plugin_id, external_id)` but not via FK.

## Tables

### `audit_entries` — owned by `core/audit_log`

Append-only event timeline. Every meaningful action lands here.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | always set; index |
| `entity_kind` | text | `'ticket'` / `'pull_request'` / `'repo'` / `'reviewer_agent'` / `'lesson'` / `'review_job'` / `'webhook_event'` / `'workspace'` / etc. |
| `entity_id` | UUID | loose ref — no FK constraint |
| `kind` | text | event type, follows `<entity>.<verb_past>` convention: `'review_job.prompt_sent'`, `'review_job.posted'`, `'lesson.created'`, `'review_job.cancelled'` |
| `payload` | JSONB | event-specific data |
| `actor_kind` | text | `'github_user'` / `'agent'` / `'system'` — matches the `ActorKind` enum |
| `actor_login` | text \| null | for `github_user` actions, the GitHub login; null otherwise |
| `actor_agent_id` | UUID \| null | for `agent` actions, the agent's id; null otherwise. **Loose ref** — no FK constraint, consistent with how `entity_id` is referenced |
| `created_at` | timestamptz | |

Indexes: `(entity_kind, entity_id, created_at)` for timeline queries; `(org_id, created_at)` for global feed. 90-day retention (auto-prune job).

### `repos` — owned by `domain/repos`

Repo allowlist.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | always set |
| `plugin_id` | text | `'github'` for M01 |
| `external_id` | text | plugin-specific identifier (`'owner/repo'`) |
| `language_hint` | text \| null | auto-detected primary language; computed once on first review (sampled from changed files) and reused; admin-clearable to trigger re-detection. Injected into prompts. |
| `status` | text | `'active'` / `'removed'` |
| `added_at`, `removed_at` | timestamptz | |

Unique: `(org_id, plugin_id, external_id)`.

### `tickets` — owned by `domain/tickets`

yaaof's unit of work.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | always set |
| `source` | text | `'github_pr'` only in M01; `'linear'` / `'jira'` / `'slack'` / `'manual'` later |
| `source_external_id` | text | original identifier from the source system |
| `title` | text | in M01, mirrors PR title |
| `description` | text | in M01, mirrors PR body |
| `status` | text | `'open'` / `'in_review'` / `'complete'` / `'abandoned'`. **M01 never uses `'open'`** — every M01 ticket is created already in `'in_review'` because the PR webhook is its trigger. `'open'` is reserved for M02+ ticket sources (Linear/Jira/Slack) that exist before any review is scheduled. |
| `repo_id` | UUID FK → `repos.id` | |
| `pr_id` | UUID FK → `pull_requests.id`, nullable | nullable in the schema for M02+; always set in M01 |
| `created_at`, `updated_at` | timestamptz | |

### `pull_requests` — owned by `domain/pull_requests`

VCS-side mirror of PRs.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | always set |
| `plugin_id` | text | `'github'` |
| `external_id` | text | plugin-specific id (e.g., `'owner/repo#123'`) |
| `repo_id` | UUID FK → `repos.id` | |
| `ticket_id` | UUID FK → `tickets.id` | always set in M01 |
| `number` | int | PR number on the VCS |
| `title`, `body` | text | |
| `author_login` | text | |
| `author_type` | text | `'user'` / `'bot'` |
| `base_branch`, `head_branch` | text | |
| `base_sha`, `head_sha` | text | |
| `is_draft`, `is_fork` | bool | |
| `state` | text | `'open'` / `'closed'` / `'merged'` |
| `html_url` | text | |
| `last_synced_at` | timestamptz | when yaaof last refreshed from the VCS |
| `created_at`, `updated_at` | timestamptz | |

Unique: `(plugin_id, external_id)`.

### `review_jobs` — owned by `domain/reviewer`

Per-PR-per-agent review job. **The row IS the durable record of one agent invocation** — yaaof has no separate task/queue table; `review_jobs` is the truth for in-flight tracking, cancellation, and crash recovery. The per-PR queue discipline (cancel/supersede/debounce) is enforced by `reviewer` on this table.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | always set |
| `pr_id` | UUID FK → `pull_requests.id` | |
| `agent_id` | UUID FK → `reviewer_agents.id` | |
| `status` | text | `'queued'` / `'running'` / `'posted'` / `'failed'` / `'skipped'` / `'cancelled'` |
| `skip_reason` | text \| null | when status=`'skipped'`: `'draft'` / `'fork'` / `'bot_author'` / `'trivial_diff'` / `'too_large'` / `'crashed'` |
| `scheduled_at`, `started_at`, `completed_at` | timestamptz \| null | |
| `last_heartbeat_at` | timestamptz \| null | bumped every 10s by the running handler; used to detect stuck jobs and drive UI progress |
| `current_step` | text \| null | free-form coarse phase label set by the handler (`'assembling_prompt'`, `'invoking_agent'`, `'posting_review'`); for UI progress |
| `prompt_hash` | text \| null | SHA256 of the assembled prompt; populated at prompt-assembly time. Matches the hash in the corresponding `review_job.prompt_sent` audit entry. Denormalized for UI read-speed. |
| `lessons_applied` | UUID[] \| null | IDs of lessons included in the prompt for this job. Populated at prompt-assembly time. UI uses these to render lesson chips on findings (via `Finding.applied_lesson_ids`) and to link from agent cards to `/memory`. |
| `tokens_in`, `tokens_out` | int \| null | populated from `AgentInvocationResult` on success |
| `cost_usd` | numeric(10,4) \| null | populated from `AgentInvocationResult` on success |
| `duration_s` | int \| null | `completed_at - started_at` computed and stored on completion; lets list views sort/aggregate without recomputation |
| `error_message` | text \| null | when status=`'failed'` |
| `review_external_id` | text \| null | when posted, the VCS-side review id |
| `created_at`, `updated_at` | timestamptz | |

Indexes:
- `(pr_id, status, created_at)` for the per-PR queue lookup.
- `(status, last_heartbeat_at)` for the `list_in_flight()` query and future stuck-job detection.

### `posted_comments` — owned by `domain/reviewer`

Maps GitHub comment ids → the agent that posted them. Written by reviewer on every successful `post_review`. Read by `intake` to resolve reply-agent lookups.

| Column | Type | Notes |
|---|---|---|
| `external_comment_id` | text | PK; the GitHub-side comment id |
| `org_id` | UUID | always set |
| `pr_id` | UUID FK → `pull_requests.id` | |
| `review_job_id` | UUID FK → `review_jobs.id` | which review attempt produced this comment |
| `agent_id` | UUID FK → `reviewer_agents.id` | denormalized for fast lookup (avoids join through review_jobs) |
| `posted_at` | timestamptz | |

Index: PK on `external_comment_id` provides O(log n) lookup by id (the only query intake makes).

### `reviewer_agents` — owned by `domain/reviewer`

Review-agent definitions. M01: 3 hardcoded rows (architecture, security, style), all using the `claude_code` plugin. M02+ `domain/implementer` will have its own `implementer_agents` table — no shared "agents" table across workflows.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | always set |
| `name` | text | `'architecture'` / `'security'` / `'style'` |
| `prompt_text` | text | the system instruction passed to the coding-agent CLI; non-empty (validated at save) |
| `coding_agent_plugin_id` | text | `'claude_code'` in M01; M02+ may have `'codex'` / `'aider'` / etc. |
| `agent_config` | JSONB | plugin-specific config (timeout, max iterations, model override, etc.). Defaults to `{}`. The plugin's `validate_config` interprets it. |
| `is_built_in` | bool | M01: true for all 3 |
| `created_at`, `updated_at` | timestamptz | |

Unique: `(org_id, name)`.

### `lessons` — owned by `domain/memory`

Per-repo lessons humans leave via the UI.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | always set |
| `repo_id` | UUID FK → `repos.id` | |
| `title` | text | short summary |
| `body` | text | ≤1000 chars (validated at save) |
| `source_pr_url` | text \| null | where the lesson originated |
| `created_at`, `updated_at` | timestamptz | |

Inline `body` (not a separate table) — single-row export.

### `github_app_installations` — owned by `plugins/github`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | always set |
| `install_external_id` | text | GitHub's installation id |
| `account_login` | text | the GitHub org/user the App is installed on |
| `status` | text | `'active'` / `'uninstalled'` |
| `created_at`, `updated_at` | timestamptz | |

### `github_settings` — owned by `plugins/github`

Singleton-ish (one row per org; one row total in M01).

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | |
| `app_id` | text | the GitHub App's numeric id |
| `encrypted_private_key` | bytea | the App's PEM, encrypted at rest |
| `encrypted_webhook_secret` | bytea | HMAC signing secret for webhook verification |
| `created_at`, `updated_at` | timestamptz | |

### `github_webhook_events` — owned by `plugins/github`

Idempotency table — records every webhook seen.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | always set; resolved from the installation that delivered the webhook |
| `source_event_id` | text | GitHub's `X-GitHub-Delivery` header; UNIQUE |
| `event_type` | text | `'pull_request'` / `'pull_request_review_comment'` / etc. |
| `received_at`, `processed_at` | timestamptz | processed_at null until dispatch completes |
| `payload` | JSONB | raw payload (post-verification) |

Unique: `source_event_id`. TTL pruning after ~30 days.

### `github_poller_state` — owned by `plugins/github`

Per-repo cursor for the catch-up poller. On startup, the plugin queries GitHub for events newer than the cursor and replays them through the normal webhook dispatch path; then advances the cursor.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | always set |
| `repo_id` | UUID FK → `repos.id` | one row per active repo |
| `last_polled_at` | timestamptz | cursor — events with `received_at` ≤ this have been processed (or were captured live via webhook) |
| `created_at`, `updated_at` | timestamptz | |

Unique: `(org_id, repo_id)`.

### `workspaces` — owned by `core/workspace`

Every workspace ever provisioned has a row here. The reaper task in `core/workspace` consults this table to enforce wall-clock caps, retry destroys, and escalate failures.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK; the workspace_id callers reference |
| `org_id` | UUID | always set |
| `provider_id` | text | `'in_process'` in M01; `'docker'` / `'fly_machine'` / etc. later |
| `spec` | JSONB | the full `WorkspaceSpec` (repo ref, sha, branch, resource caps, network policy) |
| `plugin_state` | JSONB \| null | plugin-specific data needed to destroy this workspace (e.g., `{"working_dir": "/tmp/yaaof-ws-abc123"}` for in_process; `{"container_id": "..."}` for docker). Written when status → `'active'`. |
| `status` | text | `'creating'` / `'active'` / `'expired'` / `'destroying'` / `'destroyed'` / `'destroy_failed'` |
| `created_at` | timestamptz | when the row was inserted (provisioning started) |
| `activated_at` | timestamptz \| null | when status flipped to `'active'` |
| `expires_at` | timestamptz | `activated_at + spec.wallclock_seconds`. Reaper expires workspaces past this. |
| `destroyed_at` | timestamptz \| null | when destroy completed |
| `destroy_attempts` | int | default 0; reaper increments on each plugin destroy retry. Capped at 3 in M01; past that → `destroy_failed`. |
| `last_destroy_attempt_at` | timestamptz \| null | when the most recent destroy attempt fired; reaper uses this to compute exponential backoff before next retry |
| `last_destroy_error` | text \| null | populated when destroy fails; cleared on success |

Indexes:
- `(status, expires_at)` — the reaper's primary query (find workspaces to expire/destroy).
- `(org_id, created_at DESC)` — for an ops view "show me recent workspaces" (future).

### `claude_code_settings` — owned by `plugins/claude_code`

Singleton-ish per org. Holds the Anthropic API key and any CLI-config the plugin needs to invoke `claude`.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `org_id` | UUID | |
| `encrypted_anthropic_api_key` | bytea | passed to subprocess as `ANTHROPIC_API_KEY` env var at invoke time |
| `default_model` | text \| null | optional override (e.g., `'claude-sonnet-4-6'`); else CLI uses its own default |
| `cli_path` | text \| null | optional override for the `claude` binary path; else looked up on PATH |
| `default_timeout_seconds` | int | wall-clock cap for each invocation; default 600 |
| `created_at`, `updated_at` | timestamptz | |

## Relationship notes

- **`tickets` ↔ `pull_requests` is 1-1 in M01.** Both sides reference each other (FKs). The schema permits `tickets.pr_id` to be null (for M02+ non-PR sources) but M01 always sets it. `pull_requests.ticket_id` is always set.
- **Cross-module FKs are real Postgres FKs.** Backend modules own their tables but reference other modules' tables freely via SQLAlchemy. Modularity is enforced at the import boundary, not the FK boundary.
- **`audit_entries` references entities loosely** (`entity_kind` + `entity_id`, no FK). Lets entities be deleted while the audit log survives. The trade-off: integrity is by convention, not constraint.
- **`review_jobs.agent_id` is the FK from job to agent.** The agent's prompt at job-run-time is captured in an `audit_entries` row (`kind='review_job.prompt_sent'`), not pinned by FK on the job. So prompt edits don't rewrite history.
- **`review_jobs` invariant: at most one in-flight job per `(pr_id, agent_id)`.** "In-flight" = status in (`queued`, `running`). Enforced by `reviewer`'s `PerPRQueueDiscipline` service on every schedule call, not by a DB constraint (so the audit log can retain prior cancelled/completed rows for the same pair).
- **`posted_comments` is denormalized for read speed.** `agent_id` could be reached by joining through `review_jobs`, but intake's reply-agent lookup happens on every CommentCreated event and benefits from a single-table read. Reviewer is responsible for keeping the denormalized `agent_id` consistent (only set on insert; never updated).

## Migrations

All migrations use the idempotent helpers from `core/database` (see [patterns.md](patterns.md)). Per-migration tracking via `schema_migrations`. Every migration file is single-table or single-concern. No multi-table sweeping migrations in M01.

## Decisions

### 2026-05-14 — Data model lives in one doc, but is not a module
Each module owns its tables in code. This doc is the cross-cutting picture.
**Why:** writing migrations and reasoning about FKs requires the aggregate view. Per-module docs alone don't surface relationships.

### 2026-05-14 — `org_id` everywhere from day one
Every table has `org_id` even though M01 is single-org.
**Why:** retrofitting tenancy means rewriting every query and migrating every table; pre-paying that cost is one extra column per migration.

### 2026-05-14 — No cascading deletes
Deletes are soft (`status` flips) or audit-trailed. Audit log survives entity deletion.
**Why:** the audit log is the historical record; cascades would destroy it. Soft deletes also avoid foot-guns with FK chains.

### 2026-05-14 — `audit_entries` uses loose entity references
`entity_kind` + `entity_id`, no FK.
**Why:** entities may be deleted; audit must survive. Trade integrity-by-constraint for survivability.
