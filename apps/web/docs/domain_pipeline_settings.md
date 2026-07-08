# domain/pipeline_settings

> Org Settings > Pipelines — admins compose the org's pipeline definitions: an accordion list, per-stage editor, and template instantiation.

## Scope

`/org/$slug/settings/pipelines`. Admin-only (sidebar link + backend `PIPELINES_MANAGE`, `Role.ADMIN`). Reads/writes `GET/POST /api/pipelines`, `GET/PUT/DELETE /api/pipelines/{id}`, `GET /api/pipelines/templates`, `POST /api/pipelines/from-template`, `GET /api/actions`, `GET /api/coding-agents`, `GET /api/claude_code/defaults`. Owns no data — every field it renders is server state.

## Layout

- **Accordion** (`pipelines-list`) — one row per org pipeline (`pipeline-row-${id}`), collapsed by default. The trigger shows name, stage count, a "referenced" badge (delete-block hint), and `updated <ago> by <login>`. A row's full definition is fetched lazily (`usePipelineDetail`, `enabled: true` inside the Accordion's content, which Radix only mounts while the row is open) — expanding a row is the fetch trigger.
- **"New pipeline"** — an inline card above the Accordion (`pipeline-new-card`), not an Accordion row (nothing exists server-side until Save fires the `POST`).
- **"New from template"** — a Dialog (`pipeline-template-dialog`) listing the shipped, code-defined templates; picking one calls `POST /api/pipelines/from-template`.
- **Stage list** — each stage is a row (`pipeline-stage-row-${key}`, `key` is a client-only React key, not sent to the backend) showing a kind icon, a kind `Badge`, and a one-line summary (stage name for `skill`/`review`, the action's label for `action`, the target pipeline's name for `call`). A `DropdownMenu` (`pipeline-stage-menu-${key}`) offers Move up / Move down / Remove; "Edit" (`pipeline-stage-edit-${key}`) opens the per-kind editor.
- **"Add stage"** — a `DropdownMenu` (`pipeline-add-stage`) offering the four kinds; picking one appends a blank draft and opens its editor immediately.
- **Per-kind editor** — a `Sheet` (`stage-editor`, one at a time). Fields vary by kind:
  - `skill` — name (slug), skill name, coding agent `Select`, model/effort `Select`s, a review-loop `Switch` (skill name + max iterations 1–3 + finding prefix when on), context-stages checkboxes (defaults to "all upstream"), wallclock-seconds inside a `Collapsible` "Advanced settings" section, and the boundary `RadioGroup` + conditional checkboxes + confidence `Select`.
  - `review` — same as `skill` minus the review loop (a review stage *is* the loop); carries its own finding prefix directly.
  - `action` — an action `Select` (`GET /api/actions`).
  - `call` — a `Select` of the org's other pipelines (self excluded).
- **Delete** — a `ConfirmModal` ("Delete `<name>`?" / "This can't be undone.", destructive tone — same primitive `WorkspacesSettingsPage` uses for its ARN-change confirm, not a raw `AlertDialog`). A `409 referenced` response surfaces "In use by a repo trigger or another pipeline." below the button.
- **Save errors** — a `400 invalid_definition` (stage-name collision or a call cycle, including through transitively-called pipelines) or `409 name_taken` response renders an inline `ErrorBanner` in that pipeline's editor; the definition dry-run only, so nothing partially saves.

## Picklist data

Installed coding agents (`GET /api/coding-agents`), `claude_code`'s advertised models/efforts (`GET /api/claude_code/defaults`), and registered actions (`GET /api/actions`) are fetched once at the page level (`PipelinesContent`) and threaded down as props through `PipelineEditor` → `StageEditorSheet` — keeps every `useSuspenseQuery` resolved before first paint instead of suspending deep inside a conditionally-mounted Sheet. `claude_code` is the only registered coding-agent plugin today, so the model/effort Selects read its defaults regardless of which plugin the admin picked in the agent Select.

## Editable draft shape

The wire `PipelineDefinition`/`Stage` union (`core/api/public/queries`) is what the backend accepts/returns. The editor works over a richer local "draft" shape (`types.ts`): a stable client-only `key` per stage for React lists, `reviewEnabled` instead of a nullable nested `review` object, and `contextAllUpstream` instead of a nullable `context_stages` array. `detailToDraft` / `draftToWire` convert at the editor's boundary; the server mints any stage `id` the draft omits (new pipelines and newly-added stages never carry one).

## Public interface

- `public/PipelinesSettingsPage.tsx` — `PipelinesSettingsPage` (default route component)

Private (not in `public/`): `PipelineEditor.tsx` (`ExistingPipelineEditor`, `NewPipelineCard`, the shared stage-list body), `StageEditorSheet.tsx`, `TemplateDialog.tsx`, `types.ts` (draft shapes + `draftToWire`/`detailToDraft`/validation), `queries.ts` (installed-coding-agents + claude_code-defaults hooks).

`OrgSettingsLayout` (the passthrough Org Settings shell) lives in `shared/components/public/layout/` — this module's second real consumer triggered its graduation out of `domain/org_settings` (rule-of-three, see [components.md](components.md)).

## Tests

- `test/pipelines-settings.test.tsx` — component/MSW: empty state, list rendering (name/stage-count/referenced badge), expand-to-edit lazy fetch, stage-editor per-kind field rendering, boundary-condition visibility (`conditional` mode reveals the checkboxes + confidence picker; other modes don't), "New from template" flow, 400/409 error banners.
- `apps/e2e/tests/pipeline-settings-crud.spec.ts` — Playwright: create a pipeline from the `dev` template, edit a stage's boundary to `always_proceed` and save, introduce a call cycle and see the `invalid_definition` banner, and delete a referenced pipeline to see the "In use…" message. A second test asserts a builder role sees no "Pipelines" sidebar link.
