# M02 — decisions made during autonomous run

> Append-only log of decisions made when the spec was ambiguous and certainty was below 3 of 5. Per [START_HERE.md § Decision protocol](START_HERE.md#decision-protocol).

## Format

Each entry:

```
### <Phase N> — <one-line decision summary>

- **Certainty**: <1 or 2>/5
- **Decision**: <what was chosen>
- **Alternatives considered**: <brief>
- **Why this one**: <one line>
- **Reversal cost**: <low/medium/high — how painful to undo later>
```

Keep entries terse. The user reads this at the end of the run; volume = friction.

## Entries

<!-- Append below. Do not edit prior entries. -->

### Phase 1 — M02 migration named `010_create_all_m02` (not `002_…`)

- **Certainty**: 2/5
- **Decision**: Registered the M02 create-all migration as `010_create_all_m02` in `core/database/service.py`. The spec said `002_create_all_m02`, but `002_github_settings_slug` (and 003–009) already exist from M01 maintenance migrations, so `002` would collide and break ordering.
- **Alternatives considered**: rename existing `002_…` (would invalidate every applied schema_migrations row); name it `m02_create_all` without a number (breaks the existing numeric ordering convention).
- **Why this one**: keeps strict monotonic version ordering with zero impact on already-applied DBs.
- **Reversal cost**: low — version string is only used as a registry key.

### Phase 1 — `audit_entries` gains `actor_user_id` + `actor_workspace_id` columns

- **Certainty**: 2/5
- **Decision**: The M02 migration adds two nullable UUID columns to `audit_entries` so the additive `user` / `workspace` `ActorKind` values round-trip through the audit row (existing `actor_login` / `actor_agent_id` can't carry them). `sso` actor kind uses only `actor_login` (the IdP-asserted email) since no domain id exists.
- **Alternatives considered**: pack the ids into the `payload` JSONB (cheap but loses queryability by who-did-what); add a single polymorphic `actor_subject_id` column (loses the type tagging without an extra discriminator).
- **Why this one**: keeps the columnar shape that existing per-entity audit helpers already use; nullable adds are additive and idempotent under `ADD COLUMN IF NOT EXISTS`.
- **Reversal cost**: low — additive nullable columns can be dropped without breaking reads.

### Phase 2 — auth split into `core/auth` + `domain/auth` (spec said only `core/auth`)

- **Certainty**: 2/5
- **Decision**: Pure infrastructure (middleware, contextvars, `Action` enum, `org_context()`) lives in `core/auth`. The dependency factories that actually resolve sessions/orgs/memberships (`require(action)`, `public_route`, `current_actor()`) live in `domain/auth`, which depends on `domain/identity` + `domain/orgs`.
- **Alternatives considered**: keep everything in `core/auth` and depend "upward" on `domain/*` (tach hard-blocks this — `core > domain` is a layering violation); register identity/orgs lookups into `core/auth` via a protocol shim (cleaner architecturally but adds an indirection nothing else benefits from at this stage).
- **Why this one**: `core/auth` stays pure and reusable, `domain/auth` is the natural home for "FastAPI deps that wire identity + orgs together," tach is happy.
- **Reversal cost**: low — the dep factories are pure Python; folding them back into a hypothetical `core/auth` later is a `git mv` plus an import shuffle.

### Phase 2 — middleware enforcement scoped to `M02_PROTECTED_PREFIXES`, not all of `/api/*`

- **Certainty**: 2/5
- **Decision**: The strict header check + post-response guard only apply to paths matching `M02_PROTECTED_PREFIXES` (initially `/api/account/`, `/api/memberships/`, `/api/sso/`, `/api/audit`). Legacy `/api/*` endpoints (settings, tickets, reviewer, memory, etc.) pass through unchanged so existing tests + the running app keep working through the M02 transition.
- **Alternatives considered**: ship full default-deny enforcement on all of `/api/*` in this phase (would require backfilling every existing route with `Depends(public_route)` or `Depends(require(...))` here — large unrelated diff in a Phase 2 commit); use a global feature flag (silently turns enforcement off, hides bugs).
- **Why this one**: ships the machinery + the tests asserting every middleware behavior, without breaking unrelated routes mid-milestone. Phase 14 expands the protected set to all of `/api/*` once the backfill ships.
- **Reversal cost**: low — `M02_PROTECTED_PREFIXES` is a constant in `core/auth/types.py`.

### Phase 2 — pure-ASGI middleware (not Starlette `BaseHTTPMiddleware`)

- **Certainty**: 3/5
- **Decision**: `AuthMiddleware` is implemented as a raw ASGI class with `__call__(scope, receive, send)`. Not `BaseHTTPMiddleware`.
- **Alternatives considered**: subclass `BaseHTTPMiddleware`.
- **Why this one**: `BaseHTTPMiddleware` runs the downstream app in a separate `anyio` task; contextvars set inside the route handler (`route_security_resolved = "membership"`) don't propagate back to the dispatch task, so the post-response guard can't see the dep's side effects. Pure ASGI shares the same task and contextvar mutations are visible end-to-end.
- **Reversal cost**: medium — switching back would require routing the post-response guard through `request.state` or some other shared object.

### Phase 2 — post-response guard only fires on 2xx responses

- **Certainty**: 4/5 (recorded for transparency despite the high certainty — the spec was silent here)
- **Decision**: On an M02-protected path with no `route_security_resolved`, the middleware substitutes a 500 only if the route's response status is 2xx. Non-2xx pass through.
- **Alternatives considered**: 500 unconditionally when the contextvar is unset (would mask legitimate 401/403/404 from `require()` raising `HTTPException` before setting the var with a misleading 500).
- **Why this one**: a 401 from "no session" is a legitimate response, not a missing-security bug. The guard's job is to catch "route handler returned a 200 but no security dep ran", which is the actual failure mode.
- **Reversal cost**: trivial.
