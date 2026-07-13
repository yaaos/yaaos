# domain/attachments

> Owns the `ArtifactFrontmatter` routing-metadata contract, its parser, the `ticket_attachments` table, and the HTTP routes for storing and reading ticket attachments.

## Purpose

This module owns:

- **`ArtifactFrontmatter`** — frozen Pydantic model encoding the metadata block every artifact-producing pipeline skill emits at the top of its artifact body. Committed JSON Schema copy at `.claude/skills/pipeline-schemas/artifact-frontmatter.schema.json`; `test_schema_files.py` keeps them byte-equivalent.
- **`parse_frontmatter`** — pure function that extracts and validates a leading YAML frontmatter block from a body string. Never raises; returns `None` on absence, YAML parse failure, or validation failure (context-only attachment — reaches the agent as context but is never matched by the adoption matcher).
- **`ticket_attachments` table** — one row per user-supplied ticket input document. Frontmatter is parsed at attach time; parse failure / absence leaves all metadata columns NULL.
- **`add_attachment` / `list_attachments` / `get_attachment`** — shape-a service functions (take `session: AsyncSession`, never commit).
- **HTTP routes** — `POST /api/attachments` (201) and `GET /api/attachments` (`ORG_SCOPED`).

## Public interface

Exported from `__init__.py`:

- `ArtifactFrontmatter` — routing-metadata value object.
- `parse_frontmatter(body: str) -> ArtifactFrontmatter | None` — deterministic YAML frontmatter parser.
- `Attachment` — Pydantic VO with body; returned by `add_attachment` and `get_attachment`.
- `AttachmentMeta` — Pydantic VO without body; returned by `list_attachments`.
- `add_attachment(ticket_id, *, org_id, filename, body, note, actor, session) -> Attachment`
- `list_attachments(ticket_id, *, org_id, session) -> list[AttachmentMeta]`
- `get_attachment(attachment_id, *, org_id, session) -> Attachment`
- `TicketNotFoundError`, `AttachmentTooLargeError`, `AttachmentNotFoundError`

HTTP surface: `POST /api/attachments` (201) · `GET /api/attachments?ticket_id=` (200).

## Module architecture

### Entities

- **`TicketAttachmentRow`** — one user-supplied ticket input document. Identity: `id` (UUIDv7 PK). `ticket_id` FK (`ON DELETE CASCADE`) scopes it to its ticket. `attached_at` is the precedence key for future adoption matching; `produced_by_skill` and related frontmatter columns are NULL for context-only attachments.

### Key value objects

- **`ArtifactFrontmatter`** — `frozen=True, extra="forbid"`. Fields: `yaaos_artifact_version: int`, `skill: str`, `skill_version: str`, `artifact_type: str`, `produced_at: datetime`, `repo_commit: str | None`, `produced_from: str | None`.
- **`Attachment`** — full row projection including `body`. Frozen Pydantic model.
- **`AttachmentMeta`** — row projection without `body`, for list endpoints.

### Core user flows

1. **Attach a document** — `POST /api/attachments` → `add_attachment`: size cap check (2 MiB), ticket existence check via `tickets.get`, `parse_frontmatter`, row insert, `attachment.added` audit row, `attachment_added` SSE event stashed via `publish_general_after_commit`, commit.
2. **List attachments** — `GET /api/attachments?ticket_id=` → `list_attachments`: returns `AttachmentMeta[]` newest first (`attached_at DESC, id DESC`). Bodies are never returned in the list.
3. **Get single attachment** — `get_attachment`: returns `Attachment` (with body); raises `AttachmentNotFoundError` for cross-org or absent rows (existence not leaked).
4. **Parse frontmatter from an artifact body** — caller passes the full body string to `parse_frontmatter`; returns VO on success, `None` on any failure (no error surfaced).
5. **Schema drift detection** — `test/test_schema_files.py` asserts byte-equality of committed `.claude/skills/pipeline-schemas/artifact-frontmatter.schema.json` with `ArtifactFrontmatter.model_json_schema()`.
6. **Delivery to the agent workspace** — `domain/pipelines`' run engine snapshots attachment IDs into `Kickoff.attachment_ids` at `start_manual_run` time, then materialises the files as `.yaaos-inputs/<filename>` in the provisioned workspace via the `seed-inputs` system stage (a `WriteFilesCommand` after provision; before the first skill stage). See [domain_pipelines.md § Core user flows](domain_pipelines.md#core-user-flows).

### State machines

None — no attachment state machine. Attachments are immutable once stored.

## Data owned

**`ticket_attachments`** — one row per user-supplied document.

| Column | Purpose |
|---|---|
| `id` | UUIDv7 PK, server-generated. |
| `org_id` | Org scope (NOT a FK — org rows live in a separate schema). |
| `ticket_id` | FK to `tickets.id` (CASCADE DELETE). |
| `filename` | Display name. |
| `body` | Full document text (≤ 2 MiB). |
| `produced_by_skill` | Frontmatter `skill` field; NULL = context-only. |
| `skill_version` | Frontmatter `skill_version`; NULL when no frontmatter. |
| `artifact_type` | Frontmatter `artifact_type`; NULL when no frontmatter. |
| `produced_at` | Frontmatter `produced_at`; NULL when no frontmatter. |
| `repo_commit` | Frontmatter `repo_commit`; NULL when absent or no frontmatter. |
| `produced_from` | Frontmatter `produced_from`; NULL when absent or no frontmatter. |
| `note` | Optional caller-supplied note. |
| `attached_by` | User UUID who performed the attach (`UUID(int=0)` for non-user actors). |
| `attached_at` | Server-default `now()` at insert — the adoption-matching precedence key. |

Index: `idx_ticket_attachments_match` on `(ticket_id, produced_by_skill, attached_at DESC)`.

## How it's tested

- `test/test_contracts.py` — unit tests covering the full `parse_frontmatter` decision matrix.
- `test/test_schema_files.py` — drift test asserting the committed schema file byte-equals `ArtifactFrontmatter.model_json_schema()`.
- `test/test_attachments_service.py` — service tests (`@pytest.mark.service`): frontmatter population, context-only fallback, malformed-frontmatter degradation, 2 MiB cap, unknown-ticket error, list ordering, cross-org get, `attachment.added` audit row, SSE event stash, HTTP 201/413/404, GET list endpoint. Uses real Postgres via `db_session` and `httpx.ASGITransport`.
