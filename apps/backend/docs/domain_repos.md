# domain/repos

> Per-repo protected-code + auto-approve config, and intake→pipeline trigger bindings.

## Purpose

Owns the `repo_settings` and `repo_trigger_bindings` tables. There is no `repos` table — repos are external ids from the VCS installation; the Repos-page accordion is `vcs.list_installation_repos(org_id)` (live) joined against this module's config rows. An absent `repo_settings` row means the model's defaults apply — `unconfigured` is a state, not an error. Does not yet own any runtime behavior — every `service.py` function raises `NotImplementedError`.

## Public interface

`RepoSettings` / `RepoSettingsSpec` (full config + write-input VOs), `TriggerBinding` / `TriggerBindingSpec`, `ProtectedPathSet`, `Schedule`, `ProtectedMatch`, `RepoConfigSummary`, `DueFire`, and the stub function surface (`get_settings`, `put_settings`, `list_repo_configs`, `add_binding`, `remove_binding`, `find_bindings`, `evaluate_protected`, `match_protected`, `pipeline_referenced_by_binding`, `list_due_schedule_bindings`). No HTTP routes yet.

## Module architecture

### Entities

- **RepoSettings** — per-repo protected-code + auto-approve config. Identity = `(org_id, repo_external_id)`; absent row = the model's defaults.
- **TriggerBinding** — one intake→pipeline binding for a repo, optionally carrying a `Schedule` (cron bindings).

### Key value objects

- **ProtectedPathSet** — a gitignore-style glob set + owner user ids, validated compilable at write.
- **ProtectedMatch** — the boundary's protected-code answer: `matched` + the owning user ids (deny mode: matched iff any set hits; allow mode: matched iff no set hits).
- **Schedule** — a per-repo cron trigger: name, UTC cron, notify user ids, optional kickoff input.

### Core user flows

Every service function raises `NotImplementedError` — the table and signatures are the module's current substance.

### State machines

None.

## Data owned

- `repo_settings` — `PRIMARY KEY (org_id, repo_external_id)`. `CHECK` constraint on `protected_mode` (`allow | deny`).
- `repo_trigger_bindings` — one row per binding. `pipeline_id` is a hard FK → `pipelines(id) ON DELETE RESTRICT` (DB backstop for the delete-block rule). `UNIQUE INDEX ux_bindings_point ON (org_id, repo_external_id, intake_point_id) WHERE schedule IS NULL` — schedule bindings (which can repeat per repo) are exempt.

## How it's tested

- `domain/pipelines/test/test_schema_service.py` seeds minimal `repo_settings` and `repo_trigger_bindings` rows via raw SQL (this module's service functions don't exist yet to drive them through the public API) and asserts the `repo_settings` defaults (`protected_mode='deny'`, `auto_approve_enabled=false`).
