# <one-line architecture summary>

> Current state lives in [./requirements.md § Current state](./requirements.md#current-state). This doc is target + delta only.

<!-- Target-shape rule: every `added` / `changed` interface, endpoint, table, Protocol method, and wire payload carries a full TYPE-LEVEL definition (params with types, return type, raised exceptions, schema fields, columns). Code-block formatted. Prose-only targets are refused at the lock-gate audit. Type-level ≠ implementation: NEVER paste current or target code excerpts — the cite is the current shape; the signature is the target shape. -->

## Approach

<short narrative of technical direction. Each load-bearing claim that's a *change* cites the current `file:line` it diverges from inline — e.g., "shift dispatcher from polling (`apps/backend/app/domain/reviewer/queue.py:200`) to event-driven".>

## Boundaries touched

- **Service boundaries:** <backend↔web, backend↔agent, etc.>
- **Module-within-service boundaries:** <module ↔ module>

## Entities & value objects

| Name | Kind | New/Changed | Lives in | Notes |
|---|---|---|---|---|
| <Entity> | entity / value object | new / changed | <service.module> | <new: one-line rationale. changed: `was @ path:line → is`.> |

Notes-column format: `new` rows write a one-line rationale; `changed` rows write `was @ path:line → is <new>`.

## Interface changes

> Coherence check: each boundary's add / change / delete set must form an internally consistent interface — no mixed styles, no granularity drift, no redundant endpoints, no disagreeing payload conventions.

> Every `added` / `changed` entry below carries a full type-level signature (code block). `deleted` entries carry only the `was @ path:line` cite.

### <Boundary A — e.g., backend↔agent>

**Current anchor:** `<path:line>` — <canonical current entry-point for this boundary (handler, queue consumer, route)>

#### Functions / methods

**Added:**

`<module>.<funcName>` — <one-line rationale; UC(s) exercised or "infra">

```
async def funcName(
    param_a: TypeA,
    param_b: TypeB | None = None,
    *,
    session: AsyncSession,
) -> ReturnType
# raises: ExceptionA, ExceptionB
```

**Changed:**

`<module>.<otherFunc>` — was @ `path:line`; <one-line rationale of change; UC(s) exercised>

```
async def otherFunc(
    new_param: NewType,
    *,
    session: AsyncSession,
) -> NewReturnType
# raises: NewException
```

**Deleted:**

- `<module>.<removedFunc>` — was @ `path:line`

#### HTTP endpoints

**Added:**

`POST /api/foo/{id}` — <one-line rationale; UC(s) exercised>

Request:
```
field_a: str           # required
field_b: int | null    # optional
field_c: list[UUID]    # required, non-empty
```

Response (200):
```
id: UUID                            # required
status: "queued" | "running" | "complete"   # required
created_at: ISO-8601 UTC string     # required
```

Errors: 400 invalid_payload · 404 not_found · 409 conflict

**Changed:**

`PATCH /api/bar/{id}` — was @ `path:line`; <what changed about the contract>

Request:
```
status: "active" | "archived"  # required
```

Response (200):
```
id: UUID                                  # required
status: "active" | "archived"             # required
updated_at: ISO-8601 UTC string           # required
```

Errors: 400 invalid_status · 404 not_found

**Deleted:**

- `DELETE /api/baz/{id}` — was @ `path:line`

#### Module interfaces (Protocols)

> One block per affected Protocol. List every method (added / changed / unchanged-but-relevant) so the contract is readable in one place.

**`<ProtocolName>`** — added | changed @ `path:line`

```
class ProtocolName(Protocol):
    async def method_a(
        self,
        arg: TypeA,
        *,
        session: AsyncSession,
    ) -> ResultA: ...
    # semantics: <one line — e.g., "idempotent; returns existing if duplicate">

    async def method_b(
        self,
        arg: TypeB,
    ) -> None: ...
    # raises: ProtocolErrorX
```

#### Wire payloads / events

> Cross-process / SSE / queue / IPC payloads carried over this boundary. Listed as field tables; `added` and `changed` payloads carry full field lists; `deleted` carries only the `was @ path:line`.

**Added:** `<EventName or CommandKind>` — <when emitted; who consumes>

```
command_id: UUID            # required
workspace_id: UUID          # required
kind: "InvokeClaudeCode"    # discriminator
prompt: str                 # required
limits.wallclock_seconds: int
completion_token: str       # required; agent echoes verbatim
traceparent: str | null     # W3C trace context
```

**Changed:** `<EventName>` — was @ `path:line`; <what changed>

```
+ new_field: str             # added; required
- old_field                  # removed; was: int
~ status: "v2_state_a" | "v2_state_b"  # was: int; coerced server-side
```

<repeat per boundary, each with its own Current anchor + Functions / Endpoints / Protocols / Payloads subsections as relevant — omit subsections that don't apply to this boundary>

## Sequence diagrams

<ASCII, embedded inline here, one block per use case with non-trivial sequence. Each block carries today (top) and after (bottom), separated by a horizontal rule. Cite the current entry-point `path:line` above the "today" half. Mark entities. Both states inside one block. Wire payload field lists for added/changed boundary crossings live in § Interface changes above — cross-link here rather than duplicating.>

<If no sequence changes: write "No sequence changes.">

## Use case walkthroughs

> For each use case in [./requirements.md § Use cases](./requirements.md#use-cases), trace the path through the architecture. Bullets, not prose. Names entities and interfaces from the tables above — does not redefine them.

### <actor> — <goal>

- **Trigger:** <what starts the flow>
- **Path:** <step 1: entity / interface called> → <step 2> → <step 3> → ...
- **Data crossing boundaries:** <payload names from § Interface changes — link, do not redefine>
- **Diagram:** <link to the inline block under § Sequence diagrams, or "no sequence change">

<repeat per use case — one walkthrough per use case in requirements.md>

## Data model changes

> Every added or changed table carries a full column spec (code block). Dropped tables/columns carry only the `was @ path:line` cite. Each `changed` and `dropped` row names the migration phase + (for columns) the phase that removes its last reader.

### Tables

**Added: `<table_name>`** — <one-line why this table exists>

```
id              UUID PRIMARY KEY    server_default uuidv7()
org_id          UUID NOT NULL       FK → orgs(id) ON DELETE CASCADE
status          TEXT NOT NULL       CHECK (status IN ('queued','running','complete'))
payload         JSONB NOT NULL
created_at      TIMESTAMPTZ NOT NULL  default now()
updated_at      TIMESTAMPTZ NOT NULL  default now()

INDEX idx_<table>_org_status (org_id, status)
UNIQUE (org_id, external_id)
```

**Changed: `<table_name>`** — was @ `apps/backend/app/.../models.py:<line>`; <what changed>

```
+ new_col        TEXT NOT NULL     default ''         # added
- old_col                                              # dropped; was: int; last reader removed in same phase
~ status         TEXT NOT NULL     was: enum_status   # widened; coerced server-side
```

**Dropped: `<table_name>`** — was @ `path:line`; last consumer removed same phase.

### Columns (on tables not otherwise being added/dropped)

Use the same `+ / - / ~` shape inside a code block under the table's name.

### Migrations

- **Forward:** <Alembic revision summary — what runs, idempotency notes>
- **Rollback:** <undo notes, or "irreversible after data write — gate behind feature flag until phase N">
- **Last-reader sequencing:** for every `-` column or dropped table, name the phase that removes the final reader. Drop ships in same slice as that removal — never before.

## Blocking handoff questions

> Owned by this stage. Must be empty before `/dev-plan` runs — architectural-level unknowns that block the handoff. Distinct from requirements.md and plan.md lists.

- <architectural-level unknown>

## Notes for planning

> Forward-looking material for dev-plan — slicing hints, sequencing leanings, watch-outs, and non-blocking questions that surfaced while designing. Informs but does NOT block. Capture only; do not resolve here. Self-label each bullet.

- [idea] <a slicing or sequencing leaning>
- [watch out] <a trap dev-plan should know>
- [planning-question] <non-blocking question dev-plan can resolve during phasing — NOT for the implementer; implementer-facing questions are banned in plan.md's Notes for implementation>
