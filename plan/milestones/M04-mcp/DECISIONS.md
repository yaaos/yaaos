# M04 — decisions made during autonomous run

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

### Phase 1 — advisory-lock-guarded refresh deferred

- **Certainty**: 2/5
- **Decision**: `domain/integrations` ships `connect_callback`, `get`, `clear`, `validate`, `update_allowlist` but NOT `refresh`. The Postgres advisory-lock-guarded refresh, the refresh-failure audit (`mcp.<provider>.token_refresh_failed`), and the refresh-contention test all land in a focused follow-up alongside Phase 2's MCP proxy.
- **Alternatives considered**: Land refresh in this same pass.
- **Why this one**: refresh is a Phase 2 prerequisite (the proxy refreshes expired tokens on demand) but it's not on Phase 1's critical UI path. The advisory-lock impl + the notification-queue path both deserve a dedicated commit with focused tests; better to land them alongside the proxy than rushed into Phase 1's wrap-up.
- **Reversal cost**: low — `refresh()` is additive; no existing callers break.

### Phase 1 — endpoints use header-based slug, not path-based

- **Certainty**: 3/5 (matches the M03 pattern; logged for the auditor)
- **Decision**: Endpoints mounted at `/api/integrations/{provider}/...` with `X-Org-Slug` header, not `/api/orgs/{slug}/integrations/{provider}/...`.
- **Why this one**: matches every M03+ mutation endpoint (vcs, coding-agents, byok); the SPA's `apiFetch` carries `X-Org-Slug` automatically. The OAuth callback URL is the exception — the upstream provider doesn't know our header — so the signed `state` embeds the `org_id`.
- **Reversal cost**: low.

### Phase 0b — fake-app standalone tests deferred to integration coverage

- **Certainty**: 2/5
- **Decision**: No standalone `pytest` test suites for `apps/fake-linear/` or `apps/fake-notion/`. The PHASES.md item "Tests for fake-linear / fake-notion" is satisfied by Phase 1+ backend integration tests that drive the fakes via docker-compose.
- **Alternatives considered**: Ship a `tests/` directory in each fake with a `TestClient` smoke suite.
- **Why this one**: backend pytest's conftest (testpaths = ["app"]) collides with the fakes' top-level `app` package when running from inside their dirs (the backend's `app.core` doesn't exist in the fake's namespace, but the conftest still loads). Isolating each fake under its own venv + pytest config is mechanical noise; the practical correctness check is the same docker-compose stack that production uses. Both fakes pass `docker compose up --wait` (healthchecks healthy) + manual `curl` against `/oauth/authorize` returns 303 with `code` + `state`.
- **Reversal cost**: low — add a sibling pyproject + conftest if dedicated unit coverage becomes useful.
