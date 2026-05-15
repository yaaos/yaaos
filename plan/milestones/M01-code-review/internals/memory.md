# `domain/memory` — Internal Architecture

> Per-repo lessons. Human-supplied guidance that gets injected into every future review prompt on the repo. The "remember this" feature.

## Purpose

`domain/memory` owns:

- The `lessons` table.
- CRUD operations exposed to the UI (create, list, get, update, delete).
- The retrieval API used by `reviewer` to inject lessons into the agent prompt.
- Audit-log writes on every mutation.

Small module. The complexity is in *how lessons are used* (reviewer's prompt assembly), not in storing them.

## Public interface (`__all__`)

```python
# Types
"Lesson",

# Functions
"create",
"list_for_repo",
"get",
"update",
"delete",

# Exceptions
"LessonNotFoundError",
"LessonValidationError",
```

## `Lesson` model

```python
class Lesson(BaseModel):
    id: UUID
    org_id: UUID
    repo_id: UUID
    title: str                  # short summary; scannable in list view
    body: str                   # ≤1000 chars; validated at save
    source_pr_url: str | None   # optional link to where the lesson originated
    created_at: datetime
    updated_at: datetime
```

## Public functions

### `create`

```python
async def create(
    repo_id: UUID,
    title: str,
    body: str,
    source_pr_url: str | None,
    *,
    actor: Actor,
    org_id: UUID,
) -> Lesson:
    """Create a new lesson. Validates body length (≤1000 chars), title non-empty.
    Writes audit_for_lesson(kind='lesson.created', payload=LessonCreatedPayload(title, body_length), actor=actor, org_id=org_id)."""
```

### `list_for_repo`

```python
async def list_for_repo(
    repo_id: UUID,
    *,
    org_id: UUID,
) -> list[Lesson]:
    """Returns all lessons for a repo, newest first. No pagination —
    repos have at most a few dozen lessons in practice; not a concern at M01 scale.

    Called by:
      - reviewer (every review prompt assembly)
      - UI (memory management page)"""
```

### `get`

```python
async def get(lesson_id: UUID, *, org_id: UUID) -> Lesson:
    """Raises LessonNotFoundError if not found."""
```

### `update`

```python
async def update(
    lesson_id: UUID,
    *,
    title: str | None = None,
    body: str | None = None,
    source_pr_url: str | None = None,
    actor: Actor,
    org_id: UUID,
) -> Lesson:
    """Overwrites the named fields in place; `updated_at = now()`.
    Validates body length on update.
    Writes audit_for_lesson(kind='lesson.updated',
        payload=LessonUpdatedPayload(fields_changed, prior_body_hash, new_body_hash),
        actor=actor, org_id=org_id).

    No versioning — the row is overwritten. History lives in audit_log."""
```

### `delete`

```python
async def delete(lesson_id: UUID, *, actor: Actor, org_id: UUID) -> None:
    """Hard delete (per the no-cascading-deletes rule, this is a deliberate
    hard delete of a non-referenced row).
    Writes audit_for_lesson(kind='lesson.deleted',
        payload=LessonDeletedPayload(title, body_hash_at_deletion),
        actor=actor, org_id=org_id)."""
```

Note: lessons are NOT FK'd to by any other table — `review_jobs` doesn't track which lessons it used (current lessons are assumed-applied; see the decision below). So hard delete is safe.

## Validation

```python
def _validate_lesson_fields(title: str, body: str) -> None:
    if not title or not title.strip():
        raise LessonValidationError("title is required")
    if len(title) > 200:
        raise LessonValidationError("title must be ≤200 chars")
    if not body or not body.strip():
        raise LessonValidationError("body is required")
    if len(body) > 1000:
        raise LessonValidationError("body must be ≤1000 chars (per requirements)")
```

Called on create + update.

## Retrieval semantics (called by reviewer)

When `reviewer` assembles a prompt, it calls `list_for_repo(repo_id)` and includes EVERY returned lesson in the agent prompt. There is no per-lesson relevance filtering, no scope-limiting, no per-agent subsetting in M01. Users assume that whatever lessons exist at review-time were applied.

This is the M01 simplification flagged in [requirements.md](../requirements.md): all lessons-for-repo are always used.

## What `domain/memory` does NOT do

- Does not publish events. The UI memory page re-queries after each mutation.
- Does not snapshot lesson content into audit at review time. The `review_job.prompt_sent` audit entry captures the prompt hash, not the lesson list — users assume current lessons reflect what was applied.
- Does not own scope rules beyond per-repo. M02+ may add per-agent or cross-repo scopes; the schema is designed so adding columns doesn't disrupt readers.
- Does not deduplicate. A user can create two near-identical lessons; that's the user's problem.
- Does not enforce ordering beyond `newest first`. Future UI may add manual reordering / pinning; not in M01.
- Does not allow direct creation from PR comments. (Per requirements: "PR-comment syntax for adding lessons is **not** supported in M01.")
- Does not support cross-repo "copy this lesson to other repos" bulk operations. Add when there's real demand.

## Decisions

### 2026-05-15 — Overwrite edits; history in `audit_log`
No versioning table. `audit_for_lesson(kind='lesson.updated')` captures `prior_body_hash` and `new_body_hash` so changes are traceable, but the row stores only the current state.
**Why:** versioning UI is real work for marginal value at POC scale. Audit covers the "what changed and when" question.

### 2026-05-15 — No "Memory used" UI tab; no per-lesson aggregate counters
There is no UI surface that lists which lessons were applied to which past reviews. The current lesson list is the user-facing view; lessons are assumed-applied to every review on their repo. **Per-prompt lesson IDs ARE denormalized onto `review_jobs.lessons_applied` (UUID[]) so the finding-level lesson chips (`Finding.applied_lesson_ids`) can resolve to lesson titles** — that's a UI read-speed concern, not a "memory used" timeline. **Per-lesson aggregate counters (`applied_count`) are not maintained** — there is no surface that needs them.
**Why:** lesson chips on findings are the product's primary "see how memory shaped this review" affordance; that needs IDs at job time. A full memory-used tab and per-lesson aggregate counters are bigger surfaces with marginal value at POC scale.

### 2026-05-15 — No events published from memory
Mutations are user-initiated from the memory management page; UI re-queries on success. No live broadcast.
**Why:** the memory page is admin-only and single-user-driven; live updates would be cosmetic.

### 2026-05-15 — Hard delete (not soft delete)
Lessons aren't FK'd to by any other table; deletion has no downstream effect. Audit log records the deletion (with body hash) for accountability.
