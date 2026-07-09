# domain/repo_settings

> Org Settings > Repos — admins configure trigger bindings, protected code, and PR auto-approval for each installed repo.

## Scope

`/org/$slug/settings/repos`. Admin-only (sidebar link + backend `REPOS_MANAGE`, `Role.ADMIN`). Reads/writes `GET /api/repos`, `GET /api/repos/config?repo=`, `PUT /api/repos/settings?repo=`, `POST /api/repos/triggers?repo=`, `DELETE /api/repos/triggers/{binding_id}`, `GET /api/intake/points`, `GET /api/pipelines`, `GET /api/memberships`. Owns no data — every field it renders is server state; repos themselves aren't yaaos entities, just external ids from the VCS installation.

## Layout

- **Accordion** (`repos-list`) — one row per repo `vcs.list_installation_repos` returns (`repo-row-${repo_external_id}`), collapsed by default. The trigger shows the repo id plus chips: an `unconfigured` badge when the repo has zero triggers, no protected code, and auto-approval off; otherwise a trigger-count chip and `Protected`/`Auto-approve` chips as they apply. A row's full config is fetched lazily (`useRepoConfig`, `enabled: true` inside the Accordion's content, which Radix only mounts while the row is open).
- **Triggers** (`TriggersSection.tsx`) — existing bindings list as rows (intake-point label + pipeline name; a schedule binding also shows its name/cron/notify count); "Add trigger" (`repo-add-trigger`) opens an inline form: intake-point `Select` × pipeline `Select`; picking a `schedule`-kind point reveals name `Input`, UTC cron `Input`, notify `UserMultiSelect`, and kickoff `Textarea`. Each binding commits independently (`POST`/`DELETE /api/repos/triggers`) — not part of the settings whole-section replace. Empty state: "No triggers. Nothing runs for this repo."
- **Protected code** (`ProtectedCodeSection.tsx`) — a deny/allow `RadioGroup`; switching modes opens an `AlertDialog` ("This inverts what's protected.") before the draft actually changes, since inversion is a non-obvious side effect. Path-set rows: a globs `Textarea` (one glob per line) + an owners `UserMultiSelect`; "Add path set" appends a client-minted-id row (`ProtectedPathSet.id` is required on the wire, so a new row mints its id via `crypto.randomUUID()` before Save).
- **PR auto-approval** (`AutoApprovalSection.tsx`) — a `Switch` gate plus four plain-English `Checkbox` conditions mirroring `domain/findings.AutoApproveConditions` (no open blocker / should-fix / nit findings; every posted finding confirmed fixed).
- **Save** — one button at the bottom of protected-code + auto-approval commits both via `PUT /api/repos/settings` (whole-section replace, last-write-wins). A `400 invalid_glob` renders an inline `ErrorBanner`; success shows "Saved." inline (locked copy pattern).
- **`UserMultiSelect.tsx`** — a `Popover` anchoring a filterable `Command` list with a checkmark per selected row; shared by the schedule's notify picker and the path-set owner picker.

## Picklist data

Registered intake points (`GET /api/intake/points`) and org pipelines (`GET /api/pipelines`) are fetched once at the page level (`RepoSettingsContent`) and threaded down to each row's `RepoConfigPanel`/`TriggersSection`. Org members (`GET /api/memberships`, via this module's own `useOrgMembers`) are fetched per-panel — same "second, independent consumer of the same REST surface" pattern `domain/pipeline_settings/queries.ts` uses for its coding-agent picklists (no cross-domain import; each domain module owns its own thin query hook).

## Editable draft shape

The wire `RepoConfigView` (`core/api/public/queries`) is what the backend returns. The settings form works over a local draft (`types.ts`): path-set globs flattened to one-per-line textarea text, and the loosely-typed `auto_approve_conditions` dict narrowed to the four known boolean fields. `configToDraft` / `draftToSpec` convert at the form's boundary. Trigger bindings are excluded from this draft — they're separate, immediately-committed calls.

## Public interface

- `public/RepoSettingsPage.tsx` — `RepoSettingsPage` (default route component)

Private (not in `public/`): `RepoConfigPanel.tsx` (per-row orchestration + Save), `TriggersSection.tsx`, `ProtectedCodeSection.tsx`, `AutoApprovalSection.tsx`, `UserMultiSelect.tsx`, `types.ts` (draft shapes + `configToDraft`/`draftToSpec`), `queries.ts` (`useOrgMembers`).

`OrgSettingsLayout` (the passthrough Org Settings shell) lives in `shared/components/public/layout/` — see [domain_pipeline_settings.md](domain_pipeline_settings.md).

## Tests

- `test/repo-settings.test.tsx` — component/MSW: protected-code mode-switch confirm flow (dialog blocks the flip until confirmed, cancel leaves the mode unchanged), and trigger-form schedule-field validation (schedule fields appear only for a `schedule`-kind intake point, and the submit button stays disabled until name/cron/notify are filled).
- `apps/e2e/tests/repo-settings-crud.spec.ts` — Playwright: bind `github:pr_opened` to a seeded pipeline on a repo and see the trigger chip, flip protected-code mode and see the inversion confirm, save a path set with an owner and see it round-trip through a reload, and confirm an unconfigured repo shows the `unconfigured` badge.
