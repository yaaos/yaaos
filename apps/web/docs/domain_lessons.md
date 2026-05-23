# domain/lessons

> Per-repo lessons management — teaching the agents what to look for.

## Purpose

The `/memory` page. Lists existing lessons across all repos and offers a form for adding one. Lessons are repo-scoped institutional memory (`{title, body, source_pr_url}`) that surface in reviewer prompt assembly and as applied-lesson chips on findings. Reached from the nav rail or transitively from the Teach-yaaos modal.

## Public interface

- `MemoryPage` — mounted by `core/routing` at `/memory`. Single-component module.

## Module architecture

`apps/web/src/domain/lessons/index.tsx` is a single ~130-LOC file: an "Add a lesson" form on top, a "Lessons" list below.

### Add-a-lesson form

Three inputs:
- **Repo picker** (`lesson-repo`) — `<select>` whose options are the deduplicated union of `useGithubRepositories()` and distinct `repo_external_id`s on existing lessons (covers shrunk App access).
- **Title** (`lesson-title`) — text input.
- **Body** (`lesson-body`) — textarea, `maxLength={1000}` (mirrors the backend cap).

Submit → `useCreateLesson` → `POST /api/lessons/lessons`. On success title + body clear (repo stays); on error the message renders inline.

### Lessons list

`useLessons()` (`GET /api/lessons/lessons`) returns every lesson; the API is unsorted (insertion order) and the UI preserves that. Each row shows title + repo (mono right-aligned) + Delete button; body underneath with `whitespace-pre-wrap`. Delete → `useDeleteLesson` → `DELETE /api/lessons/lessons/${id}`. No confirmation dialog.

### Cross-module entry from a finding

The Teach-yaaos modal in `domain/tickets` calls `useCreateLesson` with the finding's body pre-filled. After save, the modal closes and `["memory", repo]` is invalidated so the next nav to `/memory` shows the new lesson.

### No live updates

Lessons don't change without operator action. Queries have no `refetchInterval`; mutation invalidations are enough.

## Data owned

None. Lessons live in the backend's `memory` module.

## How it's tested

- `apps/e2e/tests/teach-yaaos-from-finding.spec.ts` — finding → modal → save → lesson appears on `/memory`.
- `apps/e2e/tests/lesson-applied-next-review.spec.ts` — seeds a lesson via `POST /api/testing/seed/lesson` (server-side), dispatches webhook, asserts `prompt_sent` audit payload's `lessons_count >= 1`.

No Vitest — the form is state-driven UI and the e2e specs cover the happy paths.
