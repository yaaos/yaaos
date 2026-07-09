# domain/repos

> Per-repo protected-code + auto-approve config, and intake→pipeline trigger bindings.

## Purpose

Owns the `repo_settings` and `repo_trigger_bindings` tables. There is no `repos` table — repos are external ids from the VCS installation; the Repos-page accordion is `vcs.list_installation_repos(org_id)` (live) joined against this module's config rows. An absent `repo_settings` row means the model's defaults apply — `unconfigured` is a state, not an error. `get_settings`/`put_settings`/`match_protected`/`evaluate_protected` are real — the protected-code config read/write path and the pure path-matching rule `domain/pipelines`' boundary evaluator composes. Trigger bindings are real too: `add_binding`/`remove_binding`/`find_bindings`/`list_repo_configs`/`pipeline_referenced_by_binding`. `list_due_schedule_bindings` is real — a global, cross-org scan (no single org to scope by) matching every `schedule`-carrying binding's UTC cron against the floored-minute slot of `now`; consumed by `domain/pipelines.pipeline_schedule_tick`.

## Public interface

`RepoSettings` / `RepoSettingsSpec` (full config + write-input VOs), `TriggerBinding` / `TriggerBindingSpec`, `PipelineRef`, `ProtectedPathSet`, `Schedule`, `ProtectedMatch`, `RepoConfigSummary`, `DueFire`, and the function surface: `get_settings`, `put_settings`, `evaluate_protected`, `match_protected`, `add_binding`, `remove_binding`, `find_bindings`, `list_repo_configs`, `pipeline_referenced_by_binding`, `register_pipeline_lookup`, `list_due_schedule_bindings` — all real. `InvalidProtectedGlobError`, `UnknownIntakePointError`, `InvalidScheduleError`, `InvalidCronError`, `DuplicateBindingError`, `UnknownPipelineError`, `BindingNotFoundError` (in `service.py`, not re-exported — intra-module) signal write-time rejections; the owning `web.py` maps each to an HTTP status. HTTP routes: `GET /api/repos`, `GET /api/repos/config?repo=`, `PUT /api/repos/settings?repo=`, `POST /api/repos/triggers?repo=`, `DELETE /api/repos/triggers/{binding_id}` — all gated on `Action.REPOS_MANAGE` (Admin).

## Module architecture

### Entities

- **RepoSettings** — per-repo protected-code + auto-approve config. Identity = `(org_id, repo_external_id)`; absent row = the model's defaults.
- **TriggerBinding** — one intake→pipeline binding for a repo, optionally carrying a `Schedule` (cron bindings).

### Key value objects

- **ProtectedPathSet** — a gitignore-style glob set + owner user ids, validated compilable (via `pathspec.GitIgnoreSpec`) at write.
- **ProtectedMatch** — the boundary's protected-code answer: `matched` + the owning user ids (deny mode: matched iff any set hits; allow mode: matched iff no set hits).
- **Schedule** — a per-repo cron trigger: name, UTC cron, notify user ids, optional kickoff input.
- **PipelineRef** — `{org_id, name}`, the minimal pipeline identity `add_binding`/`find_bindings` resolve via the registered pipeline lookup (below) — this module can't import `domain/pipelines` directly.
- **DueFire** — one due schedule firing: `{org_id, binding, fire_time}`. Carries `org_id` explicitly (not on `TriggerBinding` itself, whose accordion-read use is always already org-scoped) because `list_due_schedule_bindings` is a cross-org scan — the caller needs the firing's own org identity to open `org_context` and create the ticket in the right org.

### Core user flows

- **Read config** — `get_settings` reads the `(org_id, repo_external_id)` row (composite PK via `session.get`); an absent row projects to `RepoSettings`'s field defaults (`protected_mode="deny"`, no path sets, auto-approve off) — `unconfigured` is a state, not a 404.
- **Write config** — `put_settings` is a whole-section replace (last-write-wins, no partial patch): validates every `ProtectedPathSet.globs` entry compiles (`pathspec.GitIgnoreSpec.from_lines`, gitignore semantics — stdlib `fnmatch` mishandles `**`) raising `InvalidProtectedGlobError` on a bad pattern, validates `auto_approve_conditions` against `domain/findings.AutoApproveConditions`'s shape, upserts the row (insert-if-absent), and writes `repo.settings_updated` via `core/audit_log.audit_for_repo_settings` — keyed on `org_id` (the entity's own identity is composite, so there's no single UUID to key the audit row on; `repo_external_id` rides in the payload instead).
- **Evaluate protected-code** — `evaluate_protected` is the engine's one-call boundary read: composes `get_settings` + `match_protected`. `match_protected` is pure: **deny** mode matches iff any path hits any configured set (owners = the union of the *matched* sets' owners); **allow** mode matches iff any path escapes every set (owners = the union of *all* sets' owners, regardless of which path escaped which) — allow-mode with zero sets coherently protects everything (every path trivially escapes an empty rule list) with an empty owner set (base escalation only). Empty `paths` never matches in either mode — the boundary evaluator never even calls this when a stage reported no `paths_affected`.
- **Add a trigger binding** — `add_binding` validates, in order: `intake_point_id` is registered (`core/intake.list_intake_points()`); a `Schedule` is present iff the point's `kind == "schedule"` (and, when present, its `cron` parses via `core/tasks.CronExpr.parse`, `notify_user_ids` is non-empty and ⊆ the org's active membership); `pipeline_id` belongs to the calling org (via the registered pipeline lookup — FK alone can't check org); and, for non-schedule points, no other binding already exists for `(org, repo, intake_point_id)` (`ux_bindings_point`'s partial-unique predicate, pre-checked in the same transaction). Writes `repo.trigger_added`.
- **Remove a trigger binding** — `remove_binding` fetches + asserts org via `require_org_context()`, deletes, writes `repo.trigger_removed`.
- **Resolve bindings for a firing** — `find_bindings(org_id, repo_external_id, intake_point_id, session=)` is the read `plugins/github`'s webhook rewire calls per event; `pipeline_referenced_by_binding` is `domain/pipelines.delete_pipeline`'s reference check.
- **Resolve due schedule bindings** — `list_due_schedule_bindings(now, session=)` scans every `repo_trigger_bindings` row with a non-null `schedule` (across every org — the caller, `domain/pipelines.pipeline_schedule_tick`, has no single org to scope by), floors `now` to the minute, parses each row's `Schedule.cron` (`core/tasks.CronExpr.parse`; a malformed cron — shouldn't happen, validated at write — is skipped rather than crashing the tick), and returns one `DueFire` per matching row.

### Cross-module pipeline lookup (module boundary note)

`domain/pipelines` already depends on `domain/repos` (`pipeline_referenced_by_binding`); the reverse import would cycle. So `add_binding`'s org-ownership check and `find_bindings`'/`TriggerBinding.pipeline_name`'s name resolution go through a registered callback instead: `domain/pipelines` calls `repos.register_pipeline_lookup(fn)` once at import time (mirrors `core/api_keys.register_validator`), handing over `async (pipeline_id, session) -> PipelineRef | None`.

### State machines

None.

## Data owned

- `repo_settings` — `PRIMARY KEY (org_id, repo_external_id)`. `CHECK` constraint on `protected_mode` (`allow | deny`).
- `repo_trigger_bindings` — one row per binding. `pipeline_id` is a hard FK → `pipelines(id) ON DELETE RESTRICT` (DB backstop for the delete-block rule). `UNIQUE INDEX ux_bindings_point ON (org_id, repo_external_id, intake_point_id) WHERE schedule IS NULL` — schedule bindings (which can repeat per repo) are exempt. `schedule`'s JSONB column is declared `JSONB(none_as_null=True)` — without it, SQLAlchemy persists a Python `None` as the JSON literal `null` rather than SQL `NULL`, silently defeating both the partial index's `WHERE schedule IS NULL` predicate and `add_binding`'s own duplicate pre-check.

## How it's tested

- `test/test_match_protected.py` (unit) — the deny/allow matrices: no-paths never matches, hit vs miss, owner unions across matched (deny) or all (allow) sets, zero-sets deny never matches, zero-sets allow protects everything with no owners.
- `test/test_repo_bindings_service.py` (`@pytest.mark.service`) — binding CRUD over `/api/repos/triggers` via `httpx.ASGITransport`: success, unknown intake point, unknown/unowned pipeline, duplicate binding, invalid cron, empty `notify_user_ids`, a `schedule` payload on a non-schedule point, remove-then-404, and `domain/pipelines.delete_pipeline` returning 409 once a binding references the pipeline.
- `domain/pipelines/test/test_boundary_pause_service.py` (`@pytest.mark.service`) — `evaluate_protected` driven end-to-end through the boundary evaluator: a `put_settings`-configured protected set trips `on_protected_code` and folds the set's owner into the resulting pause's escalation set.
- `domain/pipelines/test/test_schema_service.py` seeds minimal `repo_settings` and `repo_trigger_bindings` rows via raw SQL and asserts the `repo_settings` defaults (`protected_mode='deny'`, `auto_approve_enabled=false`).
- `plugins/github/test/test_intake_rewire_service.py` covers the webhook-facing side of bound/unbound trigger resolution — see [plugins_github.md](plugins_github.md).
- `domain/pipelines/test/test_schedule_tick_service.py` covers `list_due_schedule_bindings` end-to-end through the consuming tick — see [domain_pipelines.md](domain_pipelines.md).
