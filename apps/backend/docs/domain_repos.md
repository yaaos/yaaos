# domain/repos

> Per-repo protected-code + auto-approve config, and intake→pipeline trigger bindings.

## Purpose

Owns the `repo_settings` and `repo_trigger_bindings` tables. There is no `repos` table — repos are external ids from the VCS installation; the Repos-page accordion is `vcs.list_installation_repos(org_id)` (live) joined against this module's config rows. An absent `repo_settings` row means the model's defaults apply — `unconfigured` is a state, not an error. `get_settings`/`put_settings`/`match_protected`/`evaluate_protected` are real — the protected-code config read/write path and the pure path-matching rule `domain/pipelines`' boundary evaluator composes. Trigger-binding functions (`list_repo_configs`, `add_binding`, `remove_binding`, `find_bindings`, `list_due_schedule_bindings`) stay stubs raising `NotImplementedError` — they land with the intake-rewire phase. Exception: `pipeline_referenced_by_binding` always returns `False` (no `TriggerBinding` can reference a pipeline before bindings themselves are writable) so `domain/pipelines.delete_pipeline` has a real answer to OR against its own call-stage check.

## Public interface

`RepoSettings` / `RepoSettingsSpec` (full config + write-input VOs), `TriggerBinding` / `TriggerBindingSpec`, `ProtectedPathSet`, `Schedule`, `ProtectedMatch`, `RepoConfigSummary`, `DueFire`, and the function surface: `get_settings`, `put_settings`, `evaluate_protected`, `match_protected`, `pipeline_referenced_by_binding` — real; `list_repo_configs`, `add_binding`, `remove_binding`, `find_bindings`, `list_due_schedule_bindings` — stub. `InvalidProtectedGlobError` (in `service.py`, not re-exported — intra-module) signals a malformed glob at `put_settings` write time. No HTTP routes yet.

## Module architecture

### Entities

- **RepoSettings** — per-repo protected-code + auto-approve config. Identity = `(org_id, repo_external_id)`; absent row = the model's defaults.
- **TriggerBinding** — one intake→pipeline binding for a repo, optionally carrying a `Schedule` (cron bindings).

### Key value objects

- **ProtectedPathSet** — a gitignore-style glob set + owner user ids, validated compilable (via `pathspec.GitIgnoreSpec`) at write.
- **ProtectedMatch** — the boundary's protected-code answer: `matched` + the owning user ids (deny mode: matched iff any set hits; allow mode: matched iff no set hits).
- **Schedule** — a per-repo cron trigger: name, UTC cron, notify user ids, optional kickoff input.

### Core user flows

- **Read config** — `get_settings` reads the `(org_id, repo_external_id)` row (composite PK via `session.get`); an absent row projects to `RepoSettings`'s field defaults (`protected_mode="deny"`, no path sets, auto-approve off) — `unconfigured` is a state, not a 404.
- **Write config** — `put_settings` is a whole-section replace (last-write-wins, no partial patch): validates every `ProtectedPathSet.globs` entry compiles (`pathspec.GitIgnoreSpec.from_lines`, gitignore semantics — stdlib `fnmatch` mishandles `**`) raising `InvalidProtectedGlobError` on a bad pattern, validates `auto_approve_conditions` against `domain/findings.AutoApproveConditions`'s shape, upserts the row (insert-if-absent), and writes `repo.settings_updated` via `core/audit_log.audit_for_repo_settings` — keyed on `org_id` (the entity's own identity is composite, so there's no single UUID to key the audit row on; `repo_external_id` rides in the payload instead).
- **Evaluate protected-code** — `evaluate_protected` is the engine's one-call boundary read: composes `get_settings` + `match_protected`. `match_protected` is pure: **deny** mode matches iff any path hits any configured set (owners = the union of the *matched* sets' owners); **allow** mode matches iff any path escapes every set (owners = the union of *all* sets' owners, regardless of which path escaped which) — allow-mode with zero sets coherently protects everything (every path trivially escapes an empty rule list) with an empty owner set (base escalation only). Empty `paths` never matches in either mode — the boundary evaluator never even calls this when a stage reported no `paths_affected`.

### State machines

None.

## Data owned

- `repo_settings` — `PRIMARY KEY (org_id, repo_external_id)`. `CHECK` constraint on `protected_mode` (`allow | deny`).
- `repo_trigger_bindings` — one row per binding. `pipeline_id` is a hard FK → `pipelines(id) ON DELETE RESTRICT` (DB backstop for the delete-block rule). `UNIQUE INDEX ux_bindings_point ON (org_id, repo_external_id, intake_point_id) WHERE schedule IS NULL` — schedule bindings (which can repeat per repo) are exempt.

## How it's tested

- `test/test_match_protected.py` (unit) — the deny/allow matrices: no-paths never matches, hit vs miss, owner unions across matched (deny) or all (allow) sets, zero-sets deny never matches, zero-sets allow protects everything with no owners.
- `domain/pipelines/test/test_boundary_pause_service.py` (`@pytest.mark.service`) — `evaluate_protected` driven end-to-end through the boundary evaluator: a `put_settings`-configured protected set trips `on_protected_code` and folds the set's owner into the resulting pause's escalation set.
- `domain/pipelines/test/test_schema_service.py` seeds minimal `repo_settings` and `repo_trigger_bindings` rows via raw SQL (trigger bindings still have no service surface) and asserts the `repo_settings` defaults (`protected_mode='deny'`, `auto_approve_enabled=false`).
