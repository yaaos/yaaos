# domain/pipeline_settings

> Org Settings > Pipelines — admins compose the org's pipeline definitions: an accordion list, per-stage editor, and template instantiation.

## Scope

`/org/$slug/settings/pipelines`. Admin-only (sidebar link + backend `PIPELINES_MANAGE`, `Role.ADMIN`). Reads/writes `GET/POST /api/pipelines`, `GET/PUT/DELETE /api/pipelines/{id}`, `GET /api/pipelines/templates`, `POST /api/pipelines/from-template`, `GET /api/actions`, `GET /api/coding-agents`. Owns no data — every field it renders is server state.

## Layout

- **"Download skills ({display_name})"** (`pipelines-download-skills-{plugin_id}`) — one `<a>` per installed coding agent, rendered from the `agents` picklist. Each link hits `GET /api/coding-agents/{plugin_id}/skills-bundle` (authenticated, CODING_AGENT_READ) and downloads `yaaos-pipeline-skills-{plugin_id}.zip`. The backend builds the ZIP on the fly for the requesting agent's plugin, emitting a vendor-native bundle (`claude_code` → canonical `.claude/skills/` + `.claude/agents/`; `codex` → `.codex/skills/`, `.codex/agents/*.toml`, `AGENTS.md`). A one-line caption explains what to do with the file.
- **Accordion** (`pipelines-list`) — one row per org pipeline (`pipeline-row-${id}`), collapsed by default. The trigger shows name, stage count, a "referenced" badge (delete-block hint), and `updated <ago> by <login>`. A row's full definition is fetched lazily (`usePipelineDetail`, `enabled: true` inside the Accordion's content, which Radix only mounts while the row is open) — expanding a row is the fetch trigger.
- **"New pipeline"** — an inline card above the Accordion (`pipeline-new-card`), not an Accordion row (nothing exists server-side until Save fires the `POST`).
- **"New from template"** — a Dialog (`pipeline-template-dialog`) listing the shipped, code-defined templates; picking one calls `POST /api/pipelines/from-template`.
- **Stage list** — each stage is a row (`pipeline-stage-row-${key}`, `key` is a client-only React key, not sent to the backend) showing a kind icon, a kind `Badge`, and a one-line summary (stage name for `skill`/`review`, the action's label for `action`, the target pipeline's name for `call`). A `DropdownMenu` (`pipeline-stage-menu-${key}`) offers Move up / Move down / Remove; "Edit" (`pipeline-stage-edit-${key}`) opens the per-kind editor.
- **"Add stage"** — a `DropdownMenu` (`pipeline-add-stage`) offering the four kinds; picking one appends a blank draft and opens its editor immediately. Closing that editor without saving discards the blank row (both the existing-pipeline editor and the "New pipeline" card).
- **Per-kind editor** — a `Sheet` (`stage-editor`, one at a time). Fields vary by kind:
  - `skill` — name (slug), skill name, coding agent `Select`, model/effort `Select`s, a review-loop `Switch` (skill name + max iterations 1–3 when on), context-stages checkboxes (defaults to "all upstream"), wallclock-seconds inside a `Collapsible` "Advanced settings" section, and the boundary section ("What happens after this stage completes") with a `RadioGroup` (Always pause / Always proceed automatically / Conditional) + conditional checkboxes (blocker / should-fix / nit residuals + protected-code) + confidence `Select`.
  - `review` — same as `skill` minus the review loop (a review stage *is* the loop). Finding display prefixes come from the review skill's per-finding `category`, not from stage config.
  - `action` — an action `Select` (`GET /api/actions`).
  - `call` — a `Select` of the org's other pipelines (self excluded).
- **Auto-save** — an existing pipeline persists every committed edit immediately: stage-editor "Save stage", Move up / Move down / Remove, and name/description blur each `PUT` the whole definition (`use-auto-save.ts`). Saves are serialized and coalesced — while one is in flight only the newest pending draft waits, sent when the response lands; a revert to the last-saved state during flight re-sends so server and screen converge, and an older PUT's success never overrides a newer blocked verdict. An invalid draft (empty name / no complete stage) never `PUT`s. Server-minted stage ids from the response merge back into the local draft so later saves reuse them.
- **Save status** — inline text next to Delete (`pipeline-save-status`): "Saving…" → "Saved."; a blocked-invalid draft shows "Not saved — needs a name and at least one complete stage."
- **Delete** — a `ConfirmModal` ("Delete `<name>`?" / "This can't be undone.", destructive tone — same primitive `WorkspacesSettingsPage` uses for its ARN-change confirm, not a raw `AlertDialog`). A `409 referenced` response surfaces "In use by a repo trigger or another pipeline." below the button.
- **Save errors** — a `400 invalid_definition` (stage-name collision or a call cycle, including through transitively-called pipelines) or `409 name_taken` response renders an inline `ErrorBanner` in that pipeline's editor after the offending edit's auto-save; the local draft keeps the user's edits, and the definition dry-runs server-side so nothing partially saves.

## Picklist data

Installed coding agents (`GET /api/coding-agents`) and registered actions (`GET /api/actions`) are fetched once at the page level (`PipelinesContent`) and threaded down as props through `PipelineEditor` → `StageEditorSheet` — keeps every `useSuspenseQuery` resolved before first paint instead of suspending deep inside a conditionally-mounted Sheet. Each installed-agent row carries `models`/`efforts` from the plugin's `stage_options()`; the `SkillCommonFields` component derives model/effort picker options from whichever agent the admin currently has selected in the agent `Select`, so different plugins can advertise different option sets.

## Editable draft shape

The wire `PipelineDefinition`/`Stage` union (`core/api/public/queries`) is what the backend accepts/returns. The editor works over a richer local "draft" shape (`types.ts`): a stable client-only `key` per stage for React lists, `reviewEnabled` instead of a nullable nested `review` object, and `contextAllUpstream` instead of a nullable `context_stages` array. `detailToDraft` / `draftToWire` convert at the editor's boundary; the server mints any stage `id` the draft omits (new pipelines and newly-added stages never carry one).

## Public interface

- `public/PipelinesSettingsPage.tsx` — `PipelinesSettingsPage` (default route component)

Private (not in `public/`): `PipelineEditor.tsx` (`ExistingPipelineEditor`, `NewPipelineCard`, the shared stage-list body), `StageEditorSheet.tsx`, `TemplateDialog.tsx`, `types.ts` (draft shapes + `draftToWire`/`detailToDraft`/validation), `queries.ts` (installed-coding-agents hook).

`OrgSettingsLayout` (the passthrough Org Settings shell) lives in `shared/components/public/layout/` — this module's second real consumer triggered its graduation out of `domain/org_settings` (rule-of-three, see [components.md](components.md)).

## Tests

- `test/pipelines-settings.test.tsx` — component/MSW: empty state, list rendering (name/stage-count/referenced badge), per-agent skills-download anchors' `href` (matches `/api/coding-agents/{plugin_id}/skills-bundle`) and `download` attributes, expand-to-edit lazy fetch, stage-editor per-kind field rendering, boundary-condition visibility (`conditional` mode reveals the blocker/should-fix/nit/protected checkboxes + confidence picker; other modes don't; `stage-boundary-on-nit` is the nit testid), "New from template" flow, 400/409 error banners. Auto-save: one PUT per committed edit (stage save / move / remove, name blur — not per keystroke), invalid draft blocks the save with no PUT, a rejected save shows the error banner and keeps the draft's edits, server-minted stage ids merge into the next save's body, cancelling a just-added stage discards it without a PUT (a once-saved stage survives Cancel), and no pipeline-level Save button renders on an expanded row.
- `apps/e2e/tests/pipeline-settings-crud.spec.ts` — Playwright: create a pipeline from the `dev` template; edit a stage's boundary to `always_proceed` — "Save stage" auto-saves ("Saved." status) and the edit persists across a page reload; introduce a call cycle whose stage-save auto-save surfaces the `invalid_definition` banner; delete a referenced pipeline to see the "In use…" message. A second test asserts a builder role sees no "Pipelines" sidebar link.
