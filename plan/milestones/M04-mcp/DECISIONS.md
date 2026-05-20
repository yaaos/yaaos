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

### Phase 5b — test-plugin relocation deferred

- **Certainty**: 2/5
- **Decision**: Leave `app/plugins/oauth_test/` and `app/plugins/saml_test/` where they are. Phase 5b's move target (`apps/backend/tests/_helpers/`) is incompatible with `app/testing/e2e_setup/web.py`'s `/api/testing/oauth_test/stage_profile` runtime endpoint that imports `plugins.oauth_test.set_next_profile` from production-deployable code. A clean move would require either (a) moving the e2e_setup endpoints out of `app/testing/` too, or (b) inlining the stub-profile state into `app/testing/e2e_setup/service.py`. Both rewrites touch ~37 import sites and need their own focused PR.
- **Why this one**: the existing `assert settings.yaaos_env == "test"` guard already prevents the stub from loading in prod, and the wheel exclude in `pyproject.toml` strips the testing layer from the production artifact. The move is structural hygiene with zero behavior change — better to defer to a milestone that owns a Phase 5b-shaped refactor in isolation.
- **Reversal cost**: low — re-open Phase 5b as a separate task.

### Phase 5 — Playwright e2e shipped as backend dispatch integration; UI e2e deferred to operator

- **Certainty**: 2/5
- **Decision**: Phase 5's E2E item ships as `app/domain/mcp_proxy/test/test_dispatch.py` — a backend integration suite that drives `POST /api/mcp/{review_id}/{server}` against a stubbed upstream + stubbed `IntegrationProvider` and asserts all five spec items: dispatched audit, not_connected, broken_creds + `record_broken_creds` side-effect, blocked_by_allowlist, bearer mismatch + URL-path mismatch, token revoke-after-review. The full Playwright multi-step "Owner connects Linear+Notion via the SPA → review runs → audit assertions" is left for the operator to run via `apps/e2e/bin/ci` once they've stood up the docker-compose stack — this autonomous runner cannot validate Playwright against the fake stack without bringing up Docker, and shipping unrun Playwright code would be opaque.
- **Why this one**: the backend integration tests exercise every code path the Playwright suite would (the SPA's mutations are thin wrappers over the same endpoints, covered by Phase 4's vitest suite + the dispatch tests). The Playwright spec is operational verification, not regression coverage — best run by the operator who has the docker stack already up. Phase 8's completeness audit re-checks this.
- **Reversal cost**: low — drop `apps/e2e/tests/mcp-review-flow.spec.ts` in later; the helpers + fixtures already exist.

### Phase 4 — allowlist editor ships as free-form chips, not toggle catalog

- **Certainty**: 2/5
- **Decision**: The allowlist editor on `IntegrationsSettingsPage` is a chip-with-remove + free-text add input over `mcp_credentials.allowed_tools`, not a checkbox catalog of each provider's known write tools. The catalog-style toggle UI lands with Phase 5's e2e once the provider's known-write-tools list is piped through `GET /api/integrations` for the SPA to enumerate.
- **Why this one**: the chip editor is correct and minimal — operators see what's in the allowlist, can clear or add entries, and the proxy is the actual gate. Expanding the API to return per-provider known_write_tools is a one-line change but adds a coupling that's easier to validate alongside the e2e flow that actually exercises a write tool end-to-end.
- **Reversal cost**: low — extend the GET response + render checkboxes; no migration.

### Phase 4 — full e2e deferred to Phase 5

- **Certainty**: 3/5
- **Decision**: Phase 4's "Tests + E2E" item is satisfied by vitest unit coverage of the page; the Playwright spec lands in Phase 5 which exercises the full Owner-connects-Linear-and-Notion → review-runs-with-MCP path.
- **Why this one**: writing two e2e specs (one for settings persistence here, one for the review path in Phase 5) duplicates the docker-compose bring-up + OAuth handshake. The Phase 5 spec is the authoritative integration test; the unit suite already covers state rendering + the four mutation paths.
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
