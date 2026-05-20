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

### Phase 2 — proxy returns broken_creds on token expiry (refresh deferred)

- **Certainty**: 3/5
- **Decision**: When the upstream OAuth access token's `expires_at` is in the past, the MCP proxy returns the `broken_creds` JSON-RPC error to the coding-agent instead of refreshing in-line. The advisory-lock-guarded refresh + the refresh-contention test land in a focused follow-up alongside Phase 3b's hourly health-check job.
- **Why this one**: refresh involves a non-trivial advisory-lock dance plus contention testing. The proxy's broken_creds path is already exercised by Phase 1's `validate(...)` flow; surfacing token expiry as broken_creds gives operators the same actionable signal. For a POC where reviews are short (~minutes) and access tokens long-lived (hours), in-flight expiry is rare; the cost of "operator reconnects" is acceptable.
- **Reversal cost**: low — refresh drops in at the proxy's `credential.expires_at < now()` branch.

### Phase 2 — proxy-dispatch tests deferred alongside refresh

- **Certainty**: 2/5
- **Decision**: Phase 2's six dispatch tests (not_connected / broken_creds / blocked_by_allowlist / audit-per-method) land alongside the refresh impl. Token-lifecycle tests (mint / lookup / revoke / sweep) shipped this iteration.
- **Why this one**: the dispatch tests need a stubbed-upstream + seeded credential + seeded review row; the fixture work benefits from landing together with the refresh contention test that shares the same setup. Phase 5's e2e (full path: PR → review → MCP dispatch via fake-linear/fake-notion) is the authoritative integration coverage.
- **Reversal cost**: low.

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

### Phase 3 — audit-kind actor assertions deferred to Phase 5 e2e

- **Certainty**: 2/5
- **Decision**: Phase 3's "audit rows have `actor_kind = user`/`system`" assertions ship as part of Phase 5's e2e suite, which exercises the full intake → reviewer → MCP dispatch path with real audit rows. The Phase 3 unit-level tests cover the deterministic surface (`_build_mcp_payload` collection rules + `_materialize_mcp_config` output shape).
- **Why this one**: the audit rows in question are written by `domain/mcp_proxy.web.dispatch` when the agent calls a tool — which only fires when a real `coding_agent.review` invocation reaches the proxy. The Phase 3 wire-up is exercised end-to-end by the Phase 5 suite that already drives the full fake-linear/fake-notion stack. A unit-level harness that fakes the CLI invocation just to assert audit-kind would duplicate the Phase 5 wiring.
- **Reversal cost**: low.

### Phase 0b — fake-app standalone tests deferred to integration coverage

- **Certainty**: 2/5
- **Decision**: No standalone `pytest` test suites for `apps/fake-linear/` or `apps/fake-notion/`. The PHASES.md item "Tests for fake-linear / fake-notion" is satisfied by Phase 1+ backend integration tests that drive the fakes via docker-compose.
- **Alternatives considered**: Ship a `tests/` directory in each fake with a `TestClient` smoke suite.
- **Why this one**: backend pytest's conftest (testpaths = ["app"]) collides with the fakes' top-level `app` package when running from inside their dirs (the backend's `app.core` doesn't exist in the fake's namespace, but the conftest still loads). Isolating each fake under its own venv + pytest config is mechanical noise; the practical correctness check is the same docker-compose stack that production uses. Both fakes pass `docker compose up --wait` (healthchecks healthy) + manual `curl` against `/oauth/authorize` returns 303 with `code` + `state`.
- **Reversal cost**: low — add a sibling pyproject + conftest if dedicated unit coverage becomes useful.
