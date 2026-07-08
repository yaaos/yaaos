# domain/lessons

> Per-repo lessons — human-supplied guidance injected into every future review prompt on the repo.

## Scope

Owns: `lessons` table, CRUD, audit-log writes on every mutation. No current caller retrieves lessons for prompt injection — a pipeline skill stage's prompt does not read this module today.

Does NOT own: prompt assembly, lesson versioning (history in `audit_log`), relevance filtering (all lessons for a repo are always included, when a caller does read them).

## Why / invariants

- **No versioning table.** Edits overwrite in place; `audit_log` is the history.
- **Scoped by `(plugin_id, repo_external_id)`.** No yaaos-side `repos` table; the GitHub App install governs access scope.
- **`title` ≤200 chars, `body` ≤1000 chars**, both non-blank. `LessonValidationError` → HTTP 400.
- Audit: `lesson.created` / `lesson.updated` (only when a field changed; body tracked by 16-char SHA-256 prefix) / `lesson.deleted`.

## Data owned

`lessons` — `(id, org_id, plugin_id, repo_external_id, title, body, source_pr_url, created_at, updated_at)`.

## How it's tested

- `test/test_validation.py` — empty title/body rejected, length caps, valid input passes.
- `test/test_teach_from_finding_service.py` (`@pytest.mark.service`) — `create` inserts + audits + `list_for_repo` finds it.
