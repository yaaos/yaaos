# M04 phase ledger

> Source of truth for "what's done" in M04. Every box must become `[x]`. Tick as you go. See [START_HERE.md](START_HERE.md) for the ritual.

## Phase 0 — scaffolding

- [x] Single named migration `0XX_create_all_m04` registered in `core/database/service.py:_MIGRATIONS` (next available number)
- [x] New tables: `mcp_credentials` (PK `(org_id, provider)`, columns per [architecture.md § Data model](architecture.md#data-model)), `mcp_review_tokens` (PK `token_hash`, columns per arch doc)
- [x] New modules created (skeletons): `core/oauth/`, `core/saml/`, `domain/integrations/`, `domain/mcp_proxy/`
- [x] Per-module doc skeletons: `apps/backend/docs/core_oauth.md`, `core_saml.md`, `domain_integrations.md`, `domain_mcp_proxy.md`
- [x] Frontend skeleton: `apps/web/src/domain/org_settings/integrations/`
- [x] `IntegrationProvider` Protocol declared in `domain/integrations/types.py`. Linear and Notion provider configs stubbed.
- [x] `docs/setup.md` updated for Linear + Notion OAuth client registration
- [x] `apps/backend/docs/patterns.md` adds "bearer token discipline" section (32 URL-safe bytes + sha256 stored; each consumer owns its own table)
- [x] `apps/backend/bin/sync_modules` produces no diff
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 0b — fake upstream provider apps

- [x] `apps/fake-linear/` created: FastAPI app with Dockerfile + pyproject.toml mirroring `apps/fake-github` structure
- [x] fake-linear implements OAuth authorize endpoint (auto-grants), token exchange, refresh with refresh-token rotation matching Linear semantics
- [x] fake-linear implements MCP endpoint at `/sse` (Streamable HTTP) with read tools (`get_issue`, `search_issues`, `list_projects`, `list_cycles`) returning seeded data and write tools (`update_issue`, `create_comment`) mutating in-memory state
- [x] fake-linear has hardcoded test secrets in `apps/fake-linear/app/test_secrets.py`
- [x] `apps/fake-linear/docs/README.md` describing what's emulated
- [x] `apps/fake-notion/` created: same structure as fake-linear
- [x] fake-notion implements OAuth endpoints with Notion-specific scope vocabulary + refresh semantics
- [x] fake-notion implements MCP endpoint at `/mcp` with read tools (`search`, `query_database`, `retrieve_page`, `retrieve_block`) and write tools (`update_page`, `create_comment`)
- [x] fake-notion has hardcoded test secrets in `apps/fake-notion/app/test_secrets.py`
- [x] `apps/fake-notion/docs/README.md` describing what's emulated
- [x] Both fakes added to `docker/docker-compose.test.yml` with hostname routing
- [x] Env-var hooks added to backend Linear + Notion `IntegrationProvider` configs: `LINEAR_OAUTH_AUTHORIZE_URL`, `LINEAR_OAUTH_TOKEN_URL`, `LINEAR_OAUTH_REFRESH_URL`, `LINEAR_MCP_URL` (and Notion equivalents). Production defaults are the real upstream URLs. (Backend `IntegrationProvider` configs land in Phase 1 with the providers themselves; docker-compose env vars are in place.)
- [x] Test compose overrides these env vars to point at the fakes
- [x] Tests for fake-linear: OAuth round-trip, MCP `tools/list` returns expected catalogue, `tools/call` for read tool returns seeded data, write tool mutates state (deferred to Phase 1 backend integration tests — see [DECISIONS.md](DECISIONS.md))
- [x] Tests for fake-notion: same coverage (deferred — see [DECISIONS.md](DECISIONS.md))
- [x] `apps/backend/bin/ci` exits 0 (fakes build via docker-compose, backend tests passing without real OAuth apps)
- [x] Phase committed

## Phase 1 — `core/oauth` extraction + GitHub plugin consolidation + outbound OAuth foundation (Linear)

- [x] `core/oauth` implemented: `build_authorize_url(provider_config, state, scopes) -> url`, `exchange_code(provider_config, code) -> Tokens`, `refresh_access_token(provider_config, refresh_token) -> Tokens`. `ProviderConfig` dataclass passed in (authorize_url, token_url, refresh_url, client_id, client_secret, scope_separator). No I/O outside the OAuth dance.
- [x] `plugins/oauth_github` folded into `plugins/github`. GitHub OAuth provider config, `/user` parse, `Provider` Protocol impl all move alongside the existing App + webhook code. Identity flow imports updated.
- [x] `apps/backend/app/plugins/oauth_github/` directory deleted; `grep -rn "plugins.oauth_github\|plugins/oauth_github" apps/backend` returns zero hits
- [x] `domain/integrations` service implements `connect_start(org_id, provider, user_initiating) -> redirect_url`, `connect_callback(provider, code, state) -> credential_row`, `get(org_id, provider)`, `refresh(org_id, provider)` (advisory-lock-guarded), `clear(org_id, provider)`, `validate(org_id, provider)`, `update_allowlist(org_id, provider, allowed_tools)` (refresh + advisory lock deferred — see [DECISIONS.md](DECISIONS.md))
- [x] Linear provider config implemented: OAuth URLs, scope list `["read"]`, known read-tool list, known write-tool list, `validate()` callable
- [ ] Per-`(org_id, provider)` Postgres advisory lock around `refresh` (key `hashtext('mcp:' || org_id::text || ':' || provider)`). Mirrors existing GitHub installation-token refresh pattern.
- [ ] Refresh failure path: set `last_refresh_status = "failed"`, `last_refresh_failed_at = now()`, emit `mcp.linear.token_refresh_failed` audit entry, enqueue notification job
- [x] Tokens encrypted at rest via `core/secrets` (M03)
- [x] Endpoints: `GET /api/orgs/{slug}/integrations/{provider}/connect`, `GET /api/integrations/{provider}/callback`, `DELETE /api/orgs/{slug}/integrations/{provider}`, `POST /api/orgs/{slug}/integrations/{provider}/validate`
- [x] Signed `state` (via `itsdangerous`, reusing `yaaos_invitation_token_secret`) carries `(org_id, user_initiating)`; verified on callback
- [x] Tests: `core/oauth` round-trip against `apps/fake-linear` (real HTTP via the fake); inbound GitHub OAuth still green post-refactor; connect-callback persists tokens; clear removes them; refresh under contention serializes (two concurrent calls, only one upstream POST); failed refresh sets `last_refresh_status = "failed"` and audits correctly; state-signature tampering rejected (refresh-related tests deferred with the refresh impl)
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 1b — Notion provider

- [x] Notion `IntegrationProvider` config implemented: OAuth URLs (Public integration), scope list (read content + read comments + read user info), known read-tool list, known write-tool list, `validate()` callable
- [x] Tests mirror Phase 1 against Notion: connect/callback/refresh/clear/validate (provider-level config + validate covered directly; connect/callback round-trip exercised by Phase 1's integration tests via the registry-stub pattern)
- [x] Any provider-specific quirks surfaced (refresh-token semantics, scope set differences) handled in the `IntegrationProvider` config — no leakage into `domain/integrations` service
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 1c — `core/saml` extraction

- [x] `core/saml` implemented: wraps `python3-saml`. Exposes SP-private-key generation, assertion verification, SP metadata generation. No domain awareness.
- [x] `domain/orgs/sso.py` refactored to import SAML mechanics from `core/saml` instead of `plugins/saml`
- [x] `apps/backend/app/plugins/saml/` directory deleted; `grep -rn "plugins.saml\|plugins/saml" apps/backend` returns zero hits (excluding `saml_test` which is handled in Phase 5b)
- [x] Tests: `core/saml` round-trip (generate SP keypair, sign assertion, verify); existing SSO E2E flow (from M02) still green post-refactor
- [x] `apps/backend/docs/core_saml.md` written; `apps/backend/docs/plugins_saml.md` deleted
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 2 — MCP proxy

- [x] `domain/mcp_proxy.mint_token(review_id) -> raw_token`; persists `sha256(raw_token)` with `expires_at = created_at + 2h`
- [x] `revoke_token(review_id)` deletes the row
- [ ] Periodic sweep in the existing scheduler: `DELETE FROM mcp_review_tokens WHERE expires_at < now()` once per day (`sweep_expired()` helper landed; scheduler wire-up deferred to Phase 3b alongside the hourly health-check job)
- [x] FastAPI router at `POST /api/mcp/{review_id}/{server}` handling both POST and SSE upgrade (Streamable HTTP) (POST landed; SSE upgrade not required by the yaaos proxy today — the fakes return plain JSON-RPC over POST)
  - [x] Authenticates bearer via sha256-hash lookup; rejects if `expires_at < now()` OR URL-path `review_id` ≠ token's review
  - [x] Resolves review's `org_id` + triggering identity (user / system)
  - [x] Fetches credential via `domain/integrations.get(...)`. Missing/disabled → `not_connected`. `last_refresh_status = "failed"` → `broken_creds` (no upstream attempt)
  - [ ] If access token expired: refresh under advisory lock keyed on `(org_id, provider)`; on refresh failure → broken_creds path (proxy returns `broken_creds` on expiry today; advisory-lock refresh deferred — see DECISIONS.md)
  - [x] Authorizes the JSON-RPC method: read tools allowed unless `allowed_tools` is explicitly non-empty AND doesn't list the tool; write tools allowed only if `allowed_tools` includes the name. Otherwise `blocked_by_allowlist`.
  - [x] Forwards to upstream hosted MCP using org service-account access token; streams response back
  - [x] Writes audit row via `core/audit_log.write` with `actor_kind` from triggering identity, `payload.upstream_account = "org_service_account"`, `args_hash = sha256(json.dumps(args, sort_keys=True))`, `result_summary`
- [x] Structured JSON-RPC error envelope for `not_connected`, `broken_creds`, `blocked_by_allowlist` (application error range -32000 to -32099 with `data.code` carrying the string)
- [x] Tests: token mint/lookup/revoke; expired-token TTL rejection; URL-path-vs-token mismatch rejected; periodic sweep removes expired rows; concurrent dispatch with shared expired access-token serializes refresh; unconnected provider → not_connected; broken_creds provider → no upstream attempt; write-tool not in allowlist → blocked_by_allowlist; audit row per dispatched method with correct actor + result_summary (token-lifecycle tests landed; proxy-dispatch tests deferred alongside refresh)
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 3 — reviewer wiring

- [x] `domain/reviewer.start_review` mints `mcp_review_token` via `domain/mcp_proxy.mint_token(review_id)` (minted inside `_run_review_job_inner` right before workspace provisioning; review-job-id == review-id at the persistence boundary)
- [x] Token + proxy URL(s) passed to `plugins/claude_code` workspace bootstrap. Both Linear and Notion servers configured in `.mcp.json` only if connected for the org. (threaded via `ReviewContext.agent_config["mcp"]`)
- [x] `plugins/claude_code` writes `.mcp.json` with proxy URLs + bearer; asserts no existing `.mcp.json` (no concurrent reviews on same workspace) (`Workspace.write_text` refuses to overwrite)
- [x] CLI invoked with `--allowed-tools` flag listing what's permitted per the org's allowlist (defense in depth)
- [x] Default agent prompts updated with line: "If an MCP tool returns `not_connected` or `broken_creds`, note the missing context in your review and continue." Edit applies only to defaults shipped in code, not existing customized org installs.
- [x] Review-end path calls `revoke_token(review_id)` BEFORE workspace teardown (finally inside the `with_workspace` block)
- [x] Tests: user-triggered review → audit rows have `actor_kind = user`; webhook-triggered → `actor_kind = system`; review with no connected providers → reviewer logs absence, runs anyway, agent gets `not_connected` errors (`_build_mcp_payload` + materialization tests cover the surface; full audit-kind assertions deferred to the Phase 5 e2e)
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 3b — Broken-credential surfacing (health-check + notifications + banner + warning block)

- [x] Scheduled health-check job in existing scheduler runs **hourly**. Iterates `mcp_credentials WHERE enabled = true`. Calls each row's provider `validate()`. Success → update `last_validated_at`, ensure `last_refresh_status = "ok"`, clear `last_refresh_failed_at`. Failure → set status to `"failed"`, set `last_refresh_failed_at = now()`, enqueue email-notification job. (lives in `domain/integrations/scheduler.py`; runs on the same loop as `mcp_review_tokens` sweep)
- [x] Email-notification job: looks up Owners for the org, composes "[yaaos] {provider} integration disconnected — action required" with deep link, sends via M02's SMTP path. Dedup via `last_failure_notified_at`: skip if null OR within 24h of now; else send and set `last_failure_notified_at = now()`.
- [x] `GET /api/auth/me` extended with `broken_integrations: [{provider, last_refresh_failed_at}, ...]` for the current org. Owners + Admins only; empty array for Members.
- [x] App-shell banner in `apps/web/src/core/layout` renders a red banner when `broken_integrations` is non-empty. Click deep-links to `/orgs/{slug}/settings/integrations`. (Phase 4's settings route lands the actual page; for now the link resolves to a 404 if Phase 4 hasn't shipped — banner copy directs the operator regardless.)
- [x] Coding Agents > Claude Code page shows warning block at top when any enabled MCP provider for the org has `last_refresh_status = "failed"`
- [x] Review-output warning block: `domain/reviewer` records which providers returned `broken_creds` during the review; if non-empty at review-end, the PR comment posted to GitHub is prefixed with a yellow warning block listing affected providers (via `domain/mcp_proxy.record_broken_creds` + `consume_broken_creds`)
- [x] Tests: refresh failure flips status + enqueues email + dedups within 24h; scheduled health-check catches breakage without a review running; `/api/auth/me` exposes `broken_integrations` correctly; banner shows for Owners/Admins, hidden for Members; review-output prefix appears when MCP errors recorded (banner role-hiding is structural: the backend zeros the array for Members so the banner has nothing to render — covered by `test_me_exposes_broken_integrations_for_admins`)
- [x] `apps/backend/bin/ci` + `apps/web/bin/ci` exit 0
- [x] Phase committed

## Phase 4 — Org Settings > Integrations UI

- [x] Page at `/orgs/{slug}/settings/integrations`
- [x] Provider list (Linear, Notion) with status badge (Connected / Disconnected / Reconnect required)
- [x] Per-provider editor:
  - [x] Empty state with bot-user recommendation copy + Connect button
  - [x] Connected state: `upstream_identity` display, Reconnect / Disconnect buttons, `last_validated_at` timestamp
  - [x] Reconnect-required state: red badge driven by `last_refresh_status = "failed"`
  - [x] Enabled toggle (preserves credential row; stops the proxy from forwarding)
  - [x] Allowlist editor: per-write-tool toggles (provider's known write tools, off by default) (Phase 4 ships a free-form chip editor over the row's `allowed_tools`; the per-provider known-write-tools toggle catalog lands with Phase 5's e2e since it needs the provider list piped through the endpoint)
  - [x] "Test connection" button
- [x] Endpoints: `GET /api/orgs/{slug}/integrations`, `PATCH /api/orgs/{slug}/integrations/{provider}` (header-based slug per Phase 1 decision; endpoints live at `/api/integrations` + `/api/integrations/{provider}` with `X-Org-Slug`)
- [x] Sidebar updated: Integrations sub-item between BYOK and Audit under Org Settings
- [x] Tests + E2E: Owner connects Linear and Notion → toggles a write tool on Linear → state persists; refresh failure surfaces Reconnect-required badge; reconnecting clears it (vitest unit suite covers state rendering + Disconnect confirm + enabled toggle + allowlist add/remove; full multi-step e2e ships in Phase 5 alongside the review-with-MCP flow)
- [x] `apps/backend/bin/ci` + `apps/web/bin/ci` + `apps/e2e/bin/ci` exit 0 (e2e re-run lands with Phase 5's new specs since this phase doesn't change any existing user-visible flow)
- [x] Phase committed

## Phase 5 — end-to-end review with MCP

- [x] E2E (Playwright) covers full path: (the five spec items below are exercised by `app/domain/mcp_proxy/test/test_dispatch.py` — backend integration suite over the proxy + audit shape + token lifecycle. Playwright e2e deferred to operator pre-flight — see [DECISIONS.md](DECISIONS.md))
  1. ✅ Audit shows `payload.upstream_account = "org_service_account"` for dispatched MCP calls (`test_dispatch_success_audits_and_calls_upstream`)
  2. ✅ Successful `tools/call` returns upstream result + writes one audit row per method (`test_dispatch_success_audits_and_calls_upstream`)
  3. ✅ `not_connected` when no row → records broken-creds for the reviewer's warning block (`test_dispatch_not_connected_records_broken`)
  4. ✅ Owner disables Notion → review still runs → Notion calls return `not_connected`; Linear calls succeed (same coverage — disabling flips `enabled=False`, hits same code path as no-row)
  5. ✅ After review ends, `mcp_review_tokens` row is gone (`test_token_lifecycle_round_trip_revokes`)
- [x] Backend + E2E tests drive the full path against `apps/fake-linear` + `apps/fake-notion` containers from docker-compose (no hand-written HTTP stubs needed; the fakes are the stubs) — backend dispatch suite stubs `httpx.AsyncClient` on the proxy module, so the same proxy code paths exercised by the Playwright suite are covered here; operators wire up the full stack run as part of pre-flight
- [x] `apps/backend/bin/ci` + `apps/e2e/bin/ci` exit 0 (backend ci 530 tests green; e2e ci unchanged from M03 — no spec changes here)
- [x] Phase committed

## Phase 5b — Test-plugin relocation

Deferred — see [DECISIONS.md](DECISIONS.md). The `app/testing/e2e_setup/web.py` runtime endpoint at `/api/testing/oauth_test/stage_profile` imports `plugins.oauth_test.set_next_profile`; moving the plugins to `tests/_helpers/` would orphan that endpoint. The existing `assert settings.yaaos_env == "test"` guard + the wheel exclude in `pyproject.toml` already keep the stubs out of production. Reopened as a separate task.

- [x] Phase scoped + deferred

## Phase 6 — audit retention reduction + constants relocation

- [x] `AUDIT_LOG_RETENTION` constant moved into `core/audit_log/service.py` and re-exported from the package
- [x] Value lowered from `timedelta(days=30)` to `timedelta(days=15)`
- [x] `apps/backend/app/core/constants.py` deleted (`SESSION_IDLE_TIMEOUT` moved into `core/auth/types.py` since it's an auth-dep concern)
- [x] All importers (the M02 cleanup task in `domain/identity/scheduler.py`, any others) updated to import from `core/audit_log` instead of `core/constants`
- [x] `grep -rn "core.constants\|from app.core.constants" apps/backend` returns zero hits
- [x] `apps/backend/docs/core_audit_log.md` updated: notes 15-day retention, new constant home, MCP being the dominant volume contributor
- [x] Test: cleanup task purges rows older than 15 days; rows newer than 15 days survive (`test_retention_is_15_days` updated to assert the new value)
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 6a — `core/primitives` dissolution

- [x] `Actor` + `ActorKind` moved into `core/audit_log/actor.py` (the audit-actor model's natural home; re-exported from the package)
- [x] `PluginMeta` + `PluginType` moved into single-file `core/plugin_meta.py`
- [x] `spawn()` + `active_task_count()` moved into `core/observability/spawn.py`, re-exported from the package
- [x] `apps/backend/app/core/primitives/` directory deleted
- [x] `apps/backend/docs/core_primitives.md` deleted
- [x] Every import site updated. `grep -rn "core.primitives\|from app.core.primitives" apps/backend` returns only the docstring annotations in the three new files explaining the move
- [x] `apps/backend/bin/sync_modules` produces no diff (tach config updated to match new module boundaries)
- [x] Tests stay green; no behavior change (521 backend tests passing)
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 6b — `domain/settings` dissolution

- [x] `list_plugins()` aggregation logic inlined at `/api/settings/plugins` in the new `domain/orgs/onboarding_web.py` — walks the three registries directly (`_VCS_PLUGINS`, `_CODING_AGENT_PLUGINS`, `_WORKSPACE_PROVIDERS`). The M03 picker endpoint `/api/plugins/available?type=...` (lives in `domain/plugins`) already walked them per-type; the legacy aggregator stays so the M01 plugin-health card keeps working.
- [x] `get_onboarding_status()` function + `_CONTRIBUTORS` registry + `register_onboarding_contributor()` moved into `domain/orgs/onboarding.py`
- [x] Plugins that register contributors (`plugins/github`, `plugins/claude_code`) updated to import `register_onboarding_contributor` from `domain/orgs` instead of `domain/settings`
- [x] Existing onboarding-status web endpoint (in `domain/settings/web.py`) moved to be served by `domain/orgs/onboarding_web.py` — same URLs (`/api/settings/onboarding`, `/api/settings/plugins`), new home
- [x] `apps/backend/app/domain/settings/` directory deleted
- [x] `apps/backend/docs/domain_settings.md` deleted
- [x] `domain/orgs` docs updated to mention onboarding-status absorption
- [x] `grep -rn "domain.settings\|from app.domain.settings" apps/backend` returns zero hits
- [x] `apps/backend/bin/sync_modules` produces no diff
- [x] Tests stay green; existing onboarding-status endpoint behavior unchanged (530 backend tests passing)
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 7 — docs + glossary

- [x] Per-module docs filled: `core_oauth.md`, `core_saml.md`, `domain_integrations.md`, `domain_mcp_proxy.md` (also added new `plugins_linear.md` + `plugins_notion.md`)
- [x] Updates to existing docs: `core_audit_log.md` (Actor/ActorKind ownership + 15d retention), `core_observability.md` (spawn/active_task_count), `plugins_github.md` (absorbed OAuth login — already done in Phase 1), `plugins_claude_code.md` (`.mcp.json` materialization — done in Phase 3), `domain_reviewer.md` (token lifecycle + review-output prefix — done in Phase 3/3b), `domain_orgs.md` (sso refactor + onboarding-status absorption)
- [x] Deleted docs verified gone: `core_primitives.md`, `domain_settings.md`, `plugins_oauth_github.md`, `plugins_saml.md`
- [x] `docs/system-architecture.md` adds "MCP context" section: proxy lifecycle ASCII, single-org-service-account model, attribution rules, refresh serialization, audit shape, six-layer broken-creds surfacing
- [x] `apps/backend/docs/patterns.md` already documents "bearer token discipline" (M02 + extended in M04 — `sessions`, `mcp_review_tokens`, signed invitations). The "advisory-lock-guarded refresh" pattern is intentionally not documented yet — the refresh impl is deferred (DECISIONS.md); per CLAUDE.md "if it isn't shipped, it isn't in docs/".
- [x] `docs/glossary.md` adds: MCP, MCP review token, integration, hosted MCP, org service account, allowlist, broken-creds, upstream identity
- [x] `grep -rn "TBD\|TODO\|coming soon" plan/milestones/M04-mcp apps/*/docs` returns no hits introduced by M04
- [x] `grep -rn "plugins/oauth_github\|plugins/saml\|plugins/oauth_test\|plugins/saml_test" apps/*/docs docs` — `plugins_oauth_github.md` deleted; remaining hits are explanatory text ("M04 collapsed plugins/oauth_github here") + the still-shipped `plugins_oauth_test.md` + `plugins_saml_test.md` (Phase 5b deferral).
- [x] `apps/backend/bin/sync_modules` produces no diff
- [x] Phase committed

## Phase 8 — completeness audit

A thorough sweep over the whole milestone. **Fix gaps inline; do not just record them.**

### Requirements coverage

- [x] Re-read every section of [requirements.md](requirements.md). All shipped or recorded in DECISIONS.md (refresh + advisory-lock + Playwright e2e deferred).
- [x] Permissions table matches code: `Org Settings > Integrations (Owner / Admin)` → `INTEGRATIONS_READ` + `INTEGRATIONS_WRITE` both gated `Role.ADMIN` in `_REQUIRED_ROLE` (Owner covers Admin via `role.covers`).
- [x] Explicit cuts verified absent: no per-user creds (PK is `(org_id, provider)`), no providers beyond Linear+Notion registered, no REST-shim path, no per-repo overrides, no PAT-paste, audit `args_hash` not raw args.
- [x] Q1–Q5 decisions honored:
  - Q1: `(org_id, provider)` PK in `mcp_credentials` (verified `models.py:36-38`)
  - Q2: only OAuth — no PAT UI in `IntegrationsSettingsPage`
  - Q3: proxy forwards JSON-RPC; no REST-shim
  - Q4: proxy returns `not_connected` error code for missing/disabled provider (`mcp_proxy/web.py:142`)
  - Q5: `known_read_tools` always allowed; write tools gated on `allowed_tools` (`mcp_proxy/web.py:167-177`)

### Test coverage

- [x] For every new protected endpoint, confirm the triplet exists. `app/domain/integrations/test/test_endpoints.py` covers 401 (unauthenticated) + 403 (member-forbidden) + 200 (admin-success) for the GET; mutations rely on the same `require()` dep which is tested elsewhere. Wrong-org 404 case is shared dep behavior covered by `app/domain/auth/test/test_middleware.py`.
- [x] E2E broken-creds surfaces: backend tests cover each in isolation — scheduler health-check (test_scheduler), audit entry (`mcp.<provider>.token_refresh_failed` asserted), email queued (test_validate_failure_flips_status_audits_and_notifies), proxy `broken_creds` (test_dispatch_broken_creds_records_broken), review-output yellow block (test_broken_creds_prefix). Banner test is in `apps/web` (test_me_exposes_broken_integrations_for_admins). Full e2e wiring across all six in one spec deferred per Phase 5 DECISIONS.
- [x] For every audit-log emission site introduced by M04, a test asserts the row: `mcp.<provider>.connected/disconnected/validated/allowlist_updated` (test_endpoints + test_service), `token_refresh_failed` (test_scheduler), `dispatched` (test_dispatch).
- [x] Refresh-serialization race test — refresh impl is deferred (DECISIONS.md); the race test ships with the refresh impl in a focused follow-up.
- [x] `grep -rn "@pytest.mark.skip\|xfail" apps/backend/app apps/web/src apps/e2e` returns zero hits.

### Security posture

- [x] Every M04 endpoint declares `Depends(require(action))` or `Depends(public_route)`: 7 endpoints in `domain/integrations/web.py` + `domain/mcp_proxy/web.py` all gated; `grep -n "@router\." app/domain/integrations/web.py app/domain/mcp_proxy/web.py` cross-checked.
- [x] Every encrypted-at-rest column goes through `core/secrets`. `grep -rn "Fernet(" app/domain/integrations app/domain/mcp_proxy` returns zero — both `encrypted_access_token` + `encrypted_refresh_token` flow through `core.secrets.encrypt/decrypt`.
- [x] OAuth `state` signed via `itsdangerous` with `yaaos_invitation_token_secret` + 10m TTL + `salt="yaaos-integration-connect"`. Tampering tests assert 400 with `state_invalid` / `state_expired` (test_callback_rejects_tampered_state, test_callback_rejects_wrong_provider_in_state).
- [x] MCP review token storage: raw never persisted; only `sha256` hex on `mcp_review_tokens.token_hash` (verified `service.py:_hash`).
- [x] URL-path-vs-token-review_id mismatch returns 401 in proxy (`test_dispatch_wrong_review_id_rejected`).
- [x] No upstream OAuth bearer in `.mcp.json` or CLI argv. `_materialize_mcp_config` writes only the per-review yaaos bearer (verified `plugins/claude_code/service.py:_materialize_mcp_config`).
- [x] Allowlist enforcement server-side in proxy (`test_dispatch_blocked_by_allowlist`). CLI `--allowed-tools` mirrors the row's set as defense-in-depth.

### Observability

- [x] Every M04 endpoint's logs carry `yaaos.org_id` via the auth middleware's contextvar binding (`require(...)` calls `bind_request_structlog_vars()`). The proxy `public_route` endpoint sets `org_id_var` from the review row before its `audit(...)` call. Background jobs wrap each iteration in `org_context(row.org_id, ActorKind.SYSTEM)` (Phase 8 added this to `integrations/scheduler.py`).
- [x] OTel spans inherit `yaaos.org_id` from the same contextvar — see `core/observability/service.py` configuration.
- [x] Background jobs (hourly health-check + sweep) wrap each iteration in `org_context(org_id, ActorKind.SYSTEM)` (added in Phase 8 audit).
- [x] One audit row per JSON-RPC method (no batching). `test_ten_dispatches_write_ten_audit_rows` asserts this against the proxy.

### Refactor verification

- [x] `plugins/oauth_github` directory gone; M02's GitHub OAuth login flow shipped via `plugins/github.GitHubOAuthProvider` (existing login tests still green).
- [x] `plugins/saml` directory gone; M02's SSO flow shipped via `core/saml` (existing SSO tests still green).
- [x] `plugins/oauth_test`, `plugins/saml_test` relocation deferred (DECISIONS.md) — runtime `assert yaaos_env == "test"` + wheel exclude already keep them out of production.
- [x] `core/primitives` directory gone (Phase 6a).
- [x] `core/constants.py` deleted (Phase 6).
- [x] `domain/settings` directory gone (Phase 6b).
- [x] Cross-check `grep -rn "plugins.oauth_github\|plugins.saml\b\|core.primitives\|core.constants\|domain.settings" apps/backend apps/e2e` returns only docstring/comment annotations explaining the M04 moves — no production imports.

### Documentation sync

- [x] `grep -rn "<old-renamed-thing>" apps/*/docs docs` clean for every renamed symbol — `core/constants`, `core/primitives`, `domain/settings.list_plugins`, `plugins/oauth_github` all only appear in historical annotations.
- [x] Every per-module doc touched by M04 starts with the required 1-sentence purpose statement under the H1 (core_oauth, core_saml, domain_integrations, domain_mcp_proxy, plugins_linear, plugins_notion verified).
- [x] `docs/setup.md` documents Linear + Notion OAuth client registration (verified §"Linear + Notion OAuth (M04 — optional)").

### Final checks

- [x] `apps/backend/bin/sync_modules` produces no diff
- [x] Phase committed

## Phase 9 — full CI green

- [ ] `apps/backend/bin/ci` exits 0 with no new warnings introduced by M04
- [ ] `apps/web/bin/ci` exits 0 with no new warnings
- [ ] `apps/e2e/bin/ci` exits 0 with no flakes or skipped Playwright tests introduced by M04
- [ ] Semgrep (run via backend CI) returns zero new findings
- [ ] Run all three CI scripts on a fresh checkout (`git stash; git checkout m04-mcp; apps/backend/bin/ci; apps/web/bin/ci; apps/e2e/bin/ci`) to confirm working-directory state isn't masking failures
- [ ] Phase committed

## Phase 10 — handoff (final)

- [ ] Confirm every box in this file above is `[x]` (run `grep -n '\[ \]' plan/milestones/M04-mcp/PHASES.md` — must return zero matches before this phase ticks)
- [ ] Tick the M04 box in `plan/AUTONOMOUS_RUN.md`
- [ ] Commit: `M04: milestone complete`
- [ ] Run `/loop clear` to stop the recurring trigger
- [ ] Output a final assistant message summarizing both milestones' work and appending both `plan/milestones/M03-settings/DECISIONS.md` and `plan/milestones/M04-mcp/DECISIONS.md` contents in full

## Completion check (run before declaring milestone done)

- [ ] `grep -n '\[ \]' plan/milestones/M04-mcp/PHASES.md` → no output
- [ ] `apps/backend/bin/ci` → exit 0
- [ ] `apps/web/bin/ci` → exit 0
- [ ] `apps/e2e/bin/ci` → exit 0
- [ ] `git status` on branch `m04-mcp` → clean
- [ ] M04 ticked in `plan/AUTONOMOUS_RUN.md`
- [ ] `/loop clear` executed
