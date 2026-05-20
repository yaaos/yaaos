# M04 implementation plan

> Phased build order. Read [requirements.md](requirements.md) and [architecture.md](architecture.md) first.

## Phase 0 — scaffolding

- Single named migration `0XX_create_all_m04` registered in `core/database/service.py:_MIGRATIONS` (next available number).
- New tables: `mcp_credentials` (PK `(org_id, provider)`), `mcp_review_tokens`.
- New modules: `core/oauth` (skeleton), `domain/integrations`, `domain/mcp_proxy` (skeletons + per-module doc skeletons in `apps/backend/docs/`).
- Frontend: new dir `apps/web/src/domain/org_settings/integrations`.
- `IntegrationProvider` Protocol declared in `domain/integrations.types`. Linear and Notion provider configs stubbed.
- `docs/setup.md` updated for Linear + Notion OAuth client registration (dev + prod each need their own for each provider; localhost callbacks OK).
- `apps/backend/docs/patterns.md` updated with a "bearer token discipline" section (32 URL-safe bytes + sha256 stored; each table owns its own join columns). Used as the reference point by `sessions` (M02) and `mcp_review_tokens` (this milestone) — no shared module needed.

## Phase 1 — `core/oauth` extraction + GitHub plugin consolidation + outbound OAuth foundation (Linear)

- **`core/oauth` implemented**: pure protocol mechanics. `build_authorize_url(provider_config, state, scopes)`, `exchange_code(provider_config, code) -> Tokens`, `refresh_access_token(provider_config, refresh_token) -> Tokens`. Provider config is a dataclass passed in (authorize_url, token_url, refresh_url, client_id, client_secret). No I/O outside the OAuth dance; no awareness of orgs/users/storage.
- **Fold `plugins/oauth_github` into `plugins/github`.** Delete the duplicated flow code; GitHub OAuth provider config, `/user` endpoint parse, and `Provider` Protocol impl all move alongside the existing App + webhook code in `plugins/github`. Identity-flow imports updated to point at the new location. M02's inbound login behavior unchanged.
- `domain/integrations` service implements `connect_start`, `connect_callback`, `get`, `refresh`, `clear`, `validate`, `update_allowlist`. Internally calls `core/oauth` for protocol bits and `core/secrets` for at-rest encryption.
- Linear provider implementation: OAuth provider config + scope `["read"]` + known read-tool list + known write-tool list + `validate()` callable.
- Per-`(org_id, provider)` Postgres advisory lock around `refresh`. Identical shape to existing GitHub installation-token refresh; grep for that and reuse the lock-key construction.
- Refresh failure path: set `last_refresh_status = "failed"`, set `last_refresh_failed_at`, emit `mcp.linear.token_refresh_failed` audit entry, leave the row in place so the UI can show "Reconnect required."
- Tokens encrypted at rest via `core/secrets`.
- Endpoints: `GET /api/orgs/{slug}/integrations/{provider}/connect`, `GET /api/integrations/{provider}/callback`, `DELETE /api/orgs/{slug}/integrations/{provider}`, `POST /api/orgs/{slug}/integrations/{provider}/validate`.
- Signed `state` carries `(org_id, user_initiating)`. Verified on callback.
- Tests: `core/oauth` round-trip (mocked provider endpoints); inbound github OAuth still green post-refactor; connect-callback persists tokens; clear removes them; refresh under contention serializes (two concurrent calls, only one upstream POST); failed refresh sets `last_refresh_status = "failed"` and audits correctly; state-signature tampering rejected.

## Phase 1b — Notion provider

- Add `plugins/integration_notion` (or however the project organizes provider implementations) following the Linear shape.
- Notion-specific config: Public integration OAuth flow (not internal-integration static-token), read scopes (read content + read comments + read user info), known read-tool list, known write-tool list. No new service code — the `IntegrationProvider` Protocol absorbs the differences.
- Tests mirror Phase 1's against Notion. Surfaces any Linear-shaped assumptions in `domain/integrations`.

## Phase 1c — `core/saml` extraction

- **`core/saml` implemented**: wraps `python3-saml`. Exposes SP-private-key generation, assertion verification, SP metadata generation. No domain awareness — assertions, keys, and metadata are pure SAML mechanics. Same posture as `core/oauth` and `core/secrets`.
- **Refactor `domain/orgs/sso.py`** to import from `core/saml` instead of `plugins/saml`. Behavior unchanged; tests stay green.
- **Delete `plugins/saml`.** The module's content has either moved to `core/saml` (mechanics) or was already in `domain/orgs/sso.py` (biz logic — exempt-Owner, JIT, satisfaction lifecycle).
- Tests: `core/saml` round-trip (generate SP keypair, sign assertion via `saml_test` helper, verify); existing SSO E2E flow (Phase 12 of M02) still green post-refactor.

## Phase 2 — MCP proxy

- `domain/mcp_proxy.mint_token(review_id) -> raw_token`; persists `sha256(raw_token)` with `review_id` and `expires_at = created_at + 2h`.
- `revoke_token(review_id)` deletes by `review_id`.
- Periodic sweep in the existing scheduler: `DELETE FROM mcp_review_tokens WHERE expires_at < now()` once per day. Catches orphans from crashed reviewers.
- FastAPI router at `POST /api/mcp/{review_id}/{server}` (handles both POST and SSE upgrade for Streamable HTTP).
  - Authenticates bearer (sha256-hash lookup). Rejects if `expires_at < now()` OR URL-path `review_id` doesn't match the token's review.
  - Resolves the review's `org_id` + triggering identity (user / system).
  - Fetches `mcp_credentials(org_id, server)` via `domain/integrations.get(...)`:
    - Missing or `enabled = false`: return structured `not_connected` JSON-RPC error.
    - `last_refresh_status = "failed"`: return structured `broken_creds` JSON-RPC error. Audit `result_summary = "broken_creds"`. Do not attempt upstream call.
  - If access token expired: refresh under the per-`(org_id, provider)` advisory lock. Refresh failure transitions row to `broken_creds` state and triggers the broken-creds path on this and subsequent calls.
  - Authorizes the JSON-RPC method:
    - Read tools (declared in provider code): always allowed unless `allowed_tools` is explicitly non-empty AND doesn't list the tool.
    - Write tools (declared in provider code): allowed only if `allowed_tools` includes the tool name. Otherwise return structured `blocked_by_allowlist` error.
  - Forwards to upstream hosted MCP using the org service-account access token.
  - Streams response back.
  - Writes one audit row per method via `core/audit_log.write` with `actor_kind` from the review's triggering identity, `payload.upstream_account = "org_service_account"`, `args_hash`, `result_summary` (`ok` / `not_connected` / `broken_creds` / `blocked_by_allowlist` / `upstream_error`).
- Tests: token mint/lookup/revoke; URL-path-vs-token-review mismatch rejected; **expired-token TTL rejection** (token whose row exists but `expires_at < now()` returns 401); **periodic sweep removes expired rows**; concurrent dispatch with shared expired access-token serializes refresh; unconnected provider returns `not_connected`; broken-creds provider returns `broken_creds` without attempting upstream; write tool not in allowlist returns `blocked_by_allowlist`; audit row written per dispatched method with correct actor + result_summary.

## Phase 3 — reviewer wiring

- `domain/reviewer.start_review` mints `mcp_review_token` via `domain/mcp_proxy.mint_token(review_id)`. No credential selection at this step.
- Passes token + proxy URL(s) to `plugins/claude_code` workspace bootstrap. Both Linear and Notion servers configured in the same `.mcp.json` if both are enabled for the org.
- `plugins/claude_code` writes `.mcp.json` with the proxy URLs + bearer; asserts no existing `.mcp.json` (no concurrent reviews on the same workspace).
- CLI invoked with `--allowed-tools` flag listing what's permitted per the org's allowlist (defense in depth on top of proxy enforcement).
- Default agent prompts updated with: "If an MCP tool returns `not_connected`, note the missing context in your review and continue."
- Review-end path calls `revoke_token(review_id)` regardless of outcome (success / fail / timeout / cancel).
- Tests: user-triggered review → audit rows have `actor_kind = user`; webhook-triggered review → audit rows have `actor_kind = system`; review for org with no connected providers → reviewer logs the absence, runs anyway, agent gets `not_connected` errors.

## Phase 3b — Broken-credential surfacing (health-check + notifications + banner + warning block)

- **Scheduled health-check job** in the existing scheduler. **Runs hourly.** Iterates `mcp_credentials WHERE enabled = true`. Calls each row's provider `validate()` (minimal upstream call). On success: update `last_validated_at`, ensure `last_refresh_status = "ok"`, clear `last_refresh_failed_at`. On failure: set `last_refresh_status = "failed"`, set `last_refresh_failed_at = now()`, enqueue email-notification job.
- **Email-notification job** (single-shot per transition): looks up Owners for the org, composes "[yaaos] {provider} integration disconnected — action required" with deep link, sends via M02's SMTP path. Dedup: skip if `last_failure_notified_at` is null or within 24h of now; else send and set `last_failure_notified_at = now()`.
- **`GET /api/auth/me` extended** with `broken_integrations: [{provider, last_refresh_failed_at}, ...]` for the current org. Owners + Admins only; empty array for Members.
- **App-shell banner** in `apps/web/src/core/layout` renders a red banner when `broken_integrations` is non-empty. Click deep-links to `/orgs/{slug}/settings/integrations`.
- **Org Settings > Integrations** badges the broken provider in red (already in Phase 4).
- **Coding Agents > Claude Code page** shows warning block at top when any enabled MCP provider for the org has `last_refresh_status = "failed"`.
- **Review-output warning block**: `domain/reviewer` records during the review which providers (if any) returned `broken_creds` errors. If non-empty at review-end, the PR comment posted to GitHub is prefixed with a yellow warning block listing the affected providers.
- Tests: refresh failure flips status + enqueues email + dedups within 24h; scheduled health-check catches breakage without a review running; `/api/auth/me` exposes broken_integrations correctly; banner shows for Owners/Admins, hidden for Members; review-output prefix appears when MCP errors recorded.

## Phase 4 — Org Settings > Integrations UI

- New page at `/orgs/$slug/settings/integrations`.
- Provider list (Linear, Notion) with status badge (Connected / Disconnected / Reconnect required).
- Per-provider editor:
  - Empty state with bot-user recommendation copy + Connect button.
  - Connected state with `upstream_identity` display, Reconnect / Disconnect buttons, last-validated timestamp.
  - Reconnect-required state with red badge (driven by `last_refresh_status = "failed"`).
  - Enabled toggle.
  - Allowlist editor: per-write-tool toggles (the provider's known write tools, off by default).
  - "Test connection" button.
- Endpoints: `GET /api/orgs/{slug}/integrations`, `PATCH /api/orgs/{slug}/integrations/{provider}`.
- Sidebar updated: Integrations sub-item between BYOK and Audit under Org Settings.
- Tests + E2E: Owner connects Linear and Notion → toggles a write tool on Linear → state persists; refresh failure surfaces Reconnect-required badge; reconnecting clears it.

## Phase 5 — end-to-end review with MCP

- E2E test exercises the full path:
  1. Owner connects Linear and Notion service-accounts; enables both; toggles one write tool on each.
  2. Manual UI-triggered review on a PR with a Linear ticket ID in the description → audit shows `actor_kind = user`, `payload.upstream_account = "org_service_account"`; Linear `get_issue` succeeds; Notion `search` succeeds.
  3. Webhook-triggered review on a PR from an outside contributor → audit shows `actor_kind = system`, same `upstream_account`.
  4. Disabled provider scenario: Owner disables Notion → review still runs → Notion calls return `not_connected`; Linear calls succeed.
  5. After review ends, `mcp_review_tokens` row is gone.
- Backend mocks the upstream Linear + Notion MCP endpoints (recorded fixtures) so tests don't depend on live providers.

## Phase 5b — Test-plugin relocation

- Move `apps/backend/app/plugins/oauth_test/` → `apps/backend/tests/_helpers/fake_oauth_provider.py`.
- Move `apps/backend/app/plugins/saml_test/` → `apps/backend/tests/_helpers/fake_saml_idp.py`.
- Update `conftest.py` to register these into the provider registry only when running tests. The runtime `assert yaaos_env == "test"` check can be removed — production code no longer imports these modules at all.
- Update any test imports.
- Verify `apps/backend/app/plugins/` no longer contains any test-only modules.
- Audit: `grep -rn "oauth_test\|saml_test" apps/backend/app/` should return zero hits.

## Phase 6 — audit retention reduction + constants consolidation

- `AUDIT_LOG_RETENTION` moved into `core/audit_log/` (lives alongside its owning module) and lowered from `timedelta(days=30)` to `timedelta(days=15)`.
- `apps/backend/app/core/constants.py` deleted. All importers (the existing M02 cleanup-task in `domain/identity/scheduler.py`) updated to import from `core/audit_log`.
- `core_audit_log.md` updated to reflect 15-day retention and the new home of the constant.
- Cleanup task continues to run; respects the new constant on next tick.
- No data migration: next cleanup-job tick prunes rows older than 15 days.
- Test: cleanup task purges rows older than 15 days; rows newer than 15 days survive.

## Phase 6a — `core/primitives` dissolution

- `Actor` + `ActorKind` move to `core/audit_log/` (audit is the actor model's natural home — it's the "who did what" of every audit row).
- `PluginMeta` + `PluginType` move to be co-located with plugin discovery. Since `domain/settings` is being dissolved in Phase 6c, the natural home is either inlined at the `/api/plugins/available` endpoint OR a new `core/registries` module. Pick at implementation time based on whether other plugin-discovery logic accumulates. **Default decision: co-locate with the registries themselves (`PluginMeta` is referenced by every registry; declaring it next to one of them is fine, e.g. in `core/audit_log` is wrong, but in a small `core/plugin_meta.py` is OK).** Runner records the choice in DECISIONS.md if certainty < 3.
- `spawn()` + `active_task_count()` move to `core/observability/` (they're fire-and-forget background-task wrappers whose primary job is logging unhandled exceptions — observability concern).
- `apps/backend/app/core/primitives/` directory deleted entirely; `apps/backend/docs/core_primitives.md` deleted.
- Every import site updated (`grep -rn "core.primitives\|from app.core.primitives" apps/backend` returns zero hits after).
- Tests stay green; no behavior change.

## Phase 6b — `domain/settings` dissolution

- `list_plugins()` aggregation: inline the registry-walk at the `/api/plugins/available?type=...` endpoint introduced in M03. The endpoint walks the registries directly; no service-layer indirection.
- `get_onboarding_status()` + the `_CONTRIBUTORS` registry + `register_onboarding_contributor()` move into `domain/orgs`. The plugins that register contributors (`plugins/github`, `plugins/claude_code`) update their imports.
- `apps/backend/app/domain/settings/` directory deleted entirely; `apps/backend/docs/domain_settings.md` deleted.
- `domain/orgs` docs updated to mention onboarding-status absorption.
- Tests stay green; existing onboarding-status endpoint (M01-shipped) moves to be served by `domain/orgs` web.py.

## Phase 7 — docs + cleanup + final verification

- Per-module docs: `domain_integrations.md`, `domain_mcp_proxy.md`. Updates to `core_audit_log.md` (new `kind` values + `actor_kind=system` cases), `plugins_claude_code.md` (`.mcp.json` materialization), `domain_reviewer.md` (attribution + token lifecycle), `domain_orgs.md` (`integration_caps`).
- `docs/system-architecture.md` adds "MCP context" section: proxy lifecycle ASCII, attribution rules, refresh serialization, audit shape.
- `apps/backend/docs/patterns.md` updated: "advisory-lock-guarded refresh" pattern documented in one place; refresh sites link to it.
- `docs/glossary.md` adds: MCP, MCP review token, integration, hosted MCP, scope (user/org).
- `apps/backend/bin/sync_modules`; full CI green; security scan clean.
- `plan/notes/mcp-context.md` deleted (promoted into this milestone).
- `plan/ROADMAP.md` updated: M04 status moved from `[planned]` to `[done]`.

## Dependency order

```
0 → 1 → 1b → 1c → 2 → 3 → 3b → 4 → 5 → 5b → 6 → 6a → 6b → 7
```

Phase 1 (Linear OAuth + GitHub plugin consolidation) lands the abstraction. Phase 1b (Notion) stress-tests it. Phase 1c (core/saml extraction) is independent of MCP but lands here for the cleanup spree. Phase 2 (proxy) depends on `get` and `refresh` from Phase 1. Phase 3 (reviewer wiring) sequences after the proxy. Phase 3b (broken-creds surfacing) needs Phase 1's refresh wiring + Phase 3's review output. Phase 4 (UI) can be built in parallel with phases 2–3b once data shape is locked but must land before Phase 5 (E2E). Phases 5b (test-plugin relocation), 6 (audit retention + constants relocation), 6a (primitives dissolution), 6b (settings dissolution) are mechanical refactors with no behavior change — land them near the end so they don't interleave with feature work. Phase 7 wraps everything in docs + final CI green.

## Cross-cutting through every phase

- TDD: failing test first.
- Triplet tests on protected endpoints (M02 pattern) — auth, role, success.
- Per-phase doc updates in the same commit as code.
- Per-phase commit + ledger tick (once execution scaffolding lands).

## Risks

- **OAuth client registration is an external step for both providers.** Operator action needed; runner cannot self-serve. Captured as a documented setup step (Phase 0).
- **Linear and Notion OAuth differ in scope set + refresh semantics.** Surface those differences inside the `IntegrationProvider` Protocol rather than letting them leak into `domain/integrations` service code.
- **Hosted MCP wire-protocol drift.** Wrap all upstream calls in structured error handling; failures audit-logged, not crashed.
- **First exercise of advisory locks across multiple subsystems.** Existing GitHub-token refresh uses them; M04 reuses the pattern. Inconsistency surfaces as silent races, so the test suite explicitly drives the race.
- **Defense-in-depth tension.** Proxy enforces allowlist server-side; CLI `--allowed-tools` enforces client-side. Keep them in sync via a single source list per credential row; don't let them drift.
- **Bot-user recommendation is non-enforced.** Orgs that connect-as-themselves and lose that employee will hit a broken integration. Acceptable; surfaced in docs and the empty-state UI.
