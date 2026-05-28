# domain/pull_requests

> VCS-mirror module — owns `pull_requests`, the PR aggregate, and `PRState`. Pure mirror state, no review logic.

## Scope

Owns: PR metadata persistence (shas, branches, draft/fork flags, title/body, sync timestamps), upsert + state-transition + read API.

Does NOT own: review jobs / per-PR queue (`reviewer`), ticket state (`tickets`), the decision to review (`intake`), outdated-comment markers (`vcs.mark_comments_outdated`). Publishes no events.

## Why / invariants

- **No state-machine validation on `update_state`.** VCS is source of truth; yaaos copies whatever the webhook says.
- **`upsert` never commits.** The caller composes ticket-insert + PR-upsert + audit + workflow-start in one transaction so the FK on `pull_requests.ticket_id` resolves before commit.
- **`ticket_id` required on insert, ignored on update.** The existing FK stays; branch renames are not modelled.
- **`list_by_ids` silently omits unknown ids** and short-circuits on empty input. Callers hold org context from their ticket fetch — no `org_id` scoping here.
- Immutable after insert: `plugin_id`, `external_id`, `number`, `repo_external_id`, `ticket_id`, `author_*`, `base_branch`, `head_branch`, `is_fork`.

## Data owned

`pull_requests` — `(id, org_id, plugin_id, external_id, …)`. Unique on `(plugin_id, external_id)`. Full schema in [core_database.md](core_database.md).

## How it's tested

- `test/test_upsert_session.py` — session-ownership (insert + update, FK safety, missing ticket_id guard).
- `test/test_service.py` (`@pytest.mark.service`) — `list_by_ids`: full match, empty input, unknown ids, partial match.
- Upsert/state/read also covered by `intake` integration tests.
