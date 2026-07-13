# domain/attachments

> Owns the `ArtifactFrontmatter` contract (the skill↔yaaos routing-metadata value object) and its deterministic YAML parser.

## Purpose

This module owns two things that ship together:

- **`ArtifactFrontmatter`** — a frozen Pydantic model encoding the metadata block every artifact-producing pipeline skill emits at the top of its artifact body. Fields: `yaaos_artifact_version`, `skill` (adoption match key), `skill_version`, `artifact_type`, `produced_at`, `repo_commit`, `produced_from`. Committed JSON Schema copy lives at `.claude/skills/pipeline-schemas/artifact-frontmatter.schema.json`; `test_schema_files.py` keeps them byte-equivalent.
- **`parse_frontmatter`** — a pure function that extracts and validates a leading YAML frontmatter block from an artifact body string. Never raises; returns `None` on absence, YAML parse failure, or validation failure (unknown fields, missing required fields). The caller treats `None` as a context-only artifact — it still reaches the coding agent as context, but the stage adoption matcher never matches it.

What this module does NOT own: ticket attachment storage, attachment delivery to the coding agent, artifact content itself. Those land in later phases as this module grows.

## Public interface

Exported from `__init__.py`:

- `ArtifactFrontmatter` — the routing-metadata value object.
- `parse_frontmatter(body: str) -> ArtifactFrontmatter | None` — deterministic YAML frontmatter parser.

No HTTP routes in this module.

## Module architecture

### Entities

None — this module ships only a value object and a pure parsing function.

### Key value objects

- **`ArtifactFrontmatter`** — `class ArtifactFrontmatter(BaseModel, frozen=True, extra="forbid")`. Fields: `yaaos_artifact_version: int`, `skill: str`, `skill_version: str`, `artifact_type: str`, `produced_at: datetime`, `repo_commit: str | None = None`, `produced_from: str | None = None`. `extra="forbid"` so unknown fields in the YAML block cause `parse_frontmatter` to return `None` rather than silently accepting unexpected data.

### Core user flows

1. **Parse frontmatter from an artifact body** — caller passes the full artifact body string to `parse_frontmatter`. The function checks for a `---\n` fence at byte 0, finds the closing `\n---` fence, extracts the YAML block, calls `yaml.safe_load`, validates via `ArtifactFrontmatter.model_validate`. Returns the VO on success; `None` on any failure.
2. **Schema drift detection** — `test/test_schema_files.py` reads the committed `.claude/skills/pipeline-schemas/artifact-frontmatter.schema.json` and asserts byte-equality with `ArtifactFrontmatter.model_json_schema()`. Changing the contract without updating the committed schema fails CI.

## Data owned

No tables. This module is pure in-memory contract + parser.

## How it's tested

- `test/test_contracts.py` — unit tests covering the full `parse_frontmatter` decision matrix: valid block, no block, malformed YAML, unknown field, missing required field, frontmatter not at byte 0, no closing fence, wrong field type.
- `test/test_schema_files.py` — drift test asserting the committed schema file byte-equals `ArtifactFrontmatter.model_json_schema()`. Mirrors `apps/backend/app/domain/pipelines/test/test_schema_files.py`.
