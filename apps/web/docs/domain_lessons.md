# domain/lessons

> Per-repo lessons management — institutional memory fed into reviewer prompt assembly.

## Scope

`/lessons` — add-a-lesson form + lesson list. Consumes `POST /api/lessons`, `GET /api/lessons`, `DELETE /api/lessons/:id`. Owns no data.

## Key behavior

- **Repo picker** — union of `useGithubRepositories()` and distinct `repo_external_id`s on existing lessons (covers repos removed from App access).
- **Body** — `maxLength=1000` mirrors backend cap.
- **List** — `useLessons(filter)` with `useSuspenseQuery`; list section is wrapped in `<Suspense>` + `<ErrorBoundary>` (skeleton while loading, `ErrorBanner` on fetch failure). Filter state (q, repo, sort) is local to the `LessonsList` component.
- **Cross-module entry** — `domain/tickets` Teach-yaaos modal pre-fills `useCreateLesson` with the finding body; on save invalidates `["lessons", repo]`.
- No `refetchInterval` — lessons only change on operator action.
- The `/lessons` route validates search params (`q`, `repo`, `sort`) via Zod in `core/routing/router.tsx`.

## Tests

- `apps/e2e/tests/teach-yaaos-from-finding.spec.ts` — finding → modal → lesson appears.
- `apps/e2e/tests/lesson-applied-next-review.spec.ts` — seeds lesson, asserts `lessons_count >= 1` in `prompt_sent` audit payload.
