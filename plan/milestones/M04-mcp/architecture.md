# M04 architecture

> Module layout, MCP proxy lifecycle, refresh-token serialization, settings UI surfaces. Read [requirements.md](requirements.md) first.

## Backend modules

- `core/oauth` — generic OAuth client mechanics extracted in M04. Pure protocol: `build_authorize_url(provider_config, state, scopes) -> url`, `exchange_code(provider_config, code) -> tokens`, `refresh_access_token(provider_config, refresh_token) -> tokens`. Provider configs (URLs, scope semantics) passed in by callers. No domain awareness; consumed by GitHub inbound OAuth + M04's outbound integration providers.
- `core/saml` — SAML SP mechanics extracted from M02's `plugins/saml`. Wraps `python3-saml`. Handles SP-private-key generation, assertion verification, metadata generation. No domain awareness. Consumed by `domain/orgs/sso.py` (existing M02 module — refactor target).
- `domain/integrations` — yaaos's concept of "the org has integrations." Owns the `mcp_credentials` table. Consumes `core/oauth` for protocol mechanics + `core/secrets` for at-rest encryption. Domain-shaped (the *concept* of an integration is yaaos-specific) while staying free of OAuth wire details. Service exposes: `connect_start(org_id, provider, user_initiating) -> redirect_url`, `connect_callback(provider, code, state) -> credential_row`, `get(org_id, provider)`, `refresh(org_id, provider)` (advisory-lock-guarded), `clear(...)`, `validate(...)`, `update_allowlist(...)`. Lifecycle decisions (broken-creds detection, refresh failure → status flip → notification) live here.
- `domain/mcp_proxy` — owns `mcp_review_tokens` + the FastAPI router that speaks MCP Streamable HTTP. Service exposes: `mint_token(review_id) -> token`, `revoke_token(review_id)`, `dispatch(token, server, json_rpc)`. Stays in domain — every concern (review-bound tokens, allowlist enforcement, audit-per-method, broken-creds surfacing) is yaaos-specific even though the MCP protocol it speaks is generic.

### Fake upstream provider apps

Mirror the existing `apps/fake-github` pattern. Each is its own FastAPI service with its own Dockerfile + pyproject.toml, runs as a container in `docker/docker-compose.test.yml`, and backend + E2E tests point at it via env vars. Used by ALL automated testing — the runner can build M04 end-to-end without real Linear/Notion OAuth apps registered.

- `apps/fake-linear` — implements Linear's OAuth endpoints (authorize / token / refresh with refresh-token rotation semantics matching real Linear) + Linear's hosted MCP endpoint at `/sse`. Streamable HTTP transport. Exposes a small set of read tools (`get_issue`, `search_issues`, `list_projects`, `list_cycles`) and write tools (`update_issue`, `create_comment`) that return seeded fake data.
- `apps/fake-notion` — same shape for Notion. OAuth endpoints + MCP at `/mcp`. Read tools (`search`, `query_database`, `retrieve_page`, `retrieve_block`) and write tools (`update_page`, `create_comment`). Provider quirks honored (different scope vocabulary, different refresh semantics where they differ from Linear).

Env-var override hooks read by backend tests + the Linear/Notion `IntegrationProvider` configs:

- `LINEAR_OAUTH_AUTHORIZE_URL`, `LINEAR_OAUTH_TOKEN_URL`, `LINEAR_OAUTH_REFRESH_URL`, `LINEAR_MCP_URL`
- `NOTION_OAUTH_AUTHORIZE_URL`, `NOTION_OAUTH_TOKEN_URL`, `NOTION_OAUTH_REFRESH_URL`, `NOTION_MCP_URL`

Default (production) URLs are the real upstream URLs. Test compose sets the env vars to point at the fakes. Hardcoded test secrets in each fake (e.g. `apps/fake-linear/app/test_secrets.py`) match what backend tests use, mirroring `apps/fake-github`'s pattern.

### Touched

- `core/audit_log` — adds new `kind` values (`mcp.<server>.<method>`, `mcp.<provider>.token_refreshed`, `mcp.<provider>.token_refresh_failed`). Already extensible; just expand the allowed value set. Also absorbs:
  - `Actor` + `ActorKind` (relocated from `core/primitives` — audit IS the actor model's home).
  - `AUDIT_LOG_RETENTION` constant (relocated from `core/constants.py` — it's audit's contract). New value: `timedelta(days=15)` (was 30 in M02).
- `core/observability` — absorbs `spawn()` + `active_task_count()` (relocated from `core/primitives` — they're fire-and-forget background-task wrappers whose job is exception logging, an observability concern).
- `domain/orgs` — absorbs `get_onboarding_status()` + the onboarding-contributor registry (relocated from `domain/settings` — "is this org configured?" is an org concern).
- `domain/reviewer` — review-start path mints `mcp_review_token` via `domain/mcp_proxy.mint_token`; review-end path revokes it. Workspace materialization step writes `.mcp.json`. Existing trigger-attribution (user / system) propagates to the audit row's `actor_kind`. Tracks broken-creds occurrences during the review for the warning-block prefix.
- `domain/orgs/sso.py` — refactored to import SAML mechanics from `core/saml` instead of `plugins/saml`. Behavior unchanged.
- `plugins/github` — absorbs `plugins/oauth_github`. GitHub-specific OAuth provider config (authorize_url, token_url, scopes), `/user` parse, and `Provider` Protocol impl move here alongside the existing App + webhook code. One plugin, three concerns: App, webhooks, OAuth login.
- `plugins/claude_code` — workspace bootstrap writes `.mcp.json` with the proxy URL + bearer; sets `--allowed-tools` flag (defense in depth on top of proxy-side allowlist enforcement). Default agent prompts updated with one line: "if an MCP tool returns `not_connected` or `broken_creds`, note the missing context in your review and continue." Edit applies only to defaults shipped in code — existing customized org installs are not patched.
- `core/registries` (or wherever plugins/providers enumerate) — adds `IntegrationProvider` Protocol so `domain/integrations` is provider-agnostic. Linear and Notion are its first implementations.

### Removed

- `plugins/oauth_github` — content folded into `plugins/github`.
- `plugins/saml` — content split: mechanics → `core/saml`, biz logic stays in `domain/orgs/sso.py`.
- `plugins/oauth_test` — moved out of `apps/backend/app/plugins/` into `apps/backend/tests/_helpers/fake_oauth_provider.py`. Registered into the provider registry only via `conftest.py`.
- `plugins/saml_test` — moved out of `apps/backend/app/plugins/` into `apps/backend/tests/_helpers/fake_saml_idp.py`. Same conftest-registration pattern.
- `core/primitives/` — directory deleted. `Actor`/`ActorKind` → `core/audit_log`. `PluginMeta`/`PluginType` → either inlined at the plugin-list endpoint or moved alongside the existing plugin registries (see M04 implementation plan). `spawn()`/`active_task_count()` → `core/observability`.
- `core/constants.py` — file deleted. `AUDIT_LOG_RETENTION` moved into `core/audit_log/` and lowered to 15 days.
- `domain/settings/` — directory deleted. `list_plugins()` aggregation logic inlined at the M03 `/api/plugins/available` endpoint. `get_onboarding_status()` + contributor registry moved into `domain/orgs`.

## Frontend modules

### New

- `apps/web/src/domain/org_settings/integrations` — Org Settings > Integrations page. Provider list (Linear, Notion) + per-provider editor: connect / reconnect / disconnect, enable toggle, upstream-identity display, per-write-tool allowlist toggles, "Test connection" button.

### Touched

- `apps/web/src/core/sidebar` — adds Integrations under Org Settings.

## Data model

- `mcp_credentials` — PK `(org_id, provider)`. `org_id uuid not null references orgs(id)`, `provider text not null`, `encrypted_access_token text not null`, `encrypted_refresh_token text`, `expires_at timestamptz not null`, `scopes text[] not null default '{}'`, `allowed_tools text[] not null default '{}'`, `enabled bool not null default true`, `upstream_identity text` (email/handle the OAuth flow returned, for display), `last_validated_at timestamptz`, `last_used_at timestamptz`, `last_refresh_status text` (nullable; `"ok"` or `"failed"`), `last_refresh_failed_at timestamptz`, `last_failure_notified_at timestamptz`, `created_at`, `updated_at`. Encryption via `core/secrets` (the shared Fernet wrapper introduced in M03).
- `mcp_review_tokens` — PK `token_hash text` (sha256 hex). `review_id uuid not null` (references `reviews.id`), `expires_at timestamptz not null` (set to `created_at + 2h` at mint), `created_at`. Raw token never persisted. Proxy checks `expires_at > now()` on every lookup; periodic sweep deletes expired rows.
- Audit (`audit_entries`, owned by `core/audit_log`) — no schema change; new `kind` string values added.

Single named migration `0XX_create_all_m04` following the project pattern (next-available number in `core/database/service.py:_MIGRATIONS`).

## Proxy lifecycle

```
review starts → domain/reviewer
              → domain/mcp_proxy.mint_token(review_id)
              → returns mcp_review_token
              → plugins/claude_code writes .mcp.json with:
                  url = "<host>/api/mcp/<review_id>/linear"
                  Authorization: Bearer <mcp_review_token>
              → CLI spawned

CLI → POST /api/mcp/<review_id>/linear  (Bearer <mcp_review_token>)
      body = JSON-RPC envelope

mcp_proxy.dispatch:
  1. lookup_token(bearer)            → review_id; verify expires_at > now() AND URL-path matches
  2. resolve_review(review_id)       → org_id, triggering identity (user / system)
  3. get_credential(org_id, provider)
     - if missing or disabled: return structured `not_connected` error
     - if last_refresh_status = "failed": return structured `broken_creds` error (no upstream call)
  4. authorize_method(allowed_tools, args)  → 200 or structured `blocked_by_allowlist` error
  5. if access_token expired: refresh (advisory lock keyed on (org_id, provider))
     - on refresh failure: flip to broken_creds path (above)
  6. forward upstream (mcp.<provider>.com) with org service-account access token
  7. stream response → caller
  8. audit_log.write(kind="mcp.<provider>.<method>", actor_kind=user|system, actor_user_id,
                     payload={..., upstream_account: "org_service_account", result_summary: "ok"|"not_connected"|"broken_creds"|"blocked_by_allowlist"|"upstream_error"})

review ends → domain/reviewer
            → domain/mcp_proxy.revoke_token(review_id)
            → workspace torn down
```

## Refresh-token serialization

- Postgres advisory lock keyed by `hashtext('mcp:' || org_id::text || ':' || provider)`.
- Inside lock: re-read `mcp_credentials` row; if `expires_at` is still in the future (concurrent refresh already updated), use the current `access_token`; else POST the provider's refresh endpoint, persist new tokens (set `last_refresh_status = "ok"`, clear `last_refresh_failed_at`), release lock.
- If refresh returns `invalid_grant` / 401 / 403: set `last_refresh_status = "failed"`, set `last_refresh_failed_at = now()`, emit audit entry `mcp.<provider>.token_refresh_failed`, enqueue an email-notification job. (Email job dedups via `last_failure_notified_at` — only sends if previous notification is more than 24h old or absent.)
- Existing GitHub installation-token refresh in the codebase already follows this discipline; reuse the pattern (same lock-key construction, same re-check shape).

## Broken-credential surfacing

Multiple surfaces, all driven by `mcp_credentials.last_refresh_status = "failed"`:

1. **Proxy `broken_creds` error**: when the proxy receives a request and the credential row has `last_refresh_status = "failed"`, it short-circuits — does NOT attempt to call upstream — and returns a structured JSON-RPC error `{"code": "broken_creds", "message": "..."}`. Audit row written with `result_summary = "broken_creds"`.
2. **In-app banner**: `GET /api/auth/me` (extended in M03) gains a `broken_integrations: [{provider, last_refresh_failed_at}, ...]` field. The SPA app shell renders a red banner for Owners + Admins when this list is non-empty.
3. **Org Settings > Integrations badge**: red badge on the provider card with text "Reconnect required."
4. **Coding Agents > Claude Code warning**: warning block at top of the page when any provider used by Claude Code is broken.
5. **Email**: triggered on transition to `"failed"` if `last_failure_notified_at` is null or older than 24h. Sent to all Owners via the SMTP path M02 ships for invitations.
6. **Review output yellow block**: `domain/reviewer` checks at review-end whether any enabled provider for the review's org was broken at any point during the review; if so, the PR comment yaaos posts starts with the warning block.

## Scheduled health-check

- New periodic job in the existing scheduler: **every 1 hour**, iterate `mcp_credentials WHERE enabled = true`. For each row, call the provider's `validate()` method (minimal upstream call).
- On success: set `last_validated_at = now()`, set `last_refresh_status = "ok"`, clear `last_refresh_failed_at`.
- On failure: set `last_refresh_status = "failed"`, set `last_refresh_failed_at = now()`, enqueue email-notification job (deduped).
- Catches breakage between reviews, closing the window between "credentials revoked upstream" and "yaaos notices."

## Review lifecycle

`domain/reviewer.start_review`:

1. Already knows: `review_id`, `org_id`, optional `triggered_by_user_id` (set when human-triggered; null for webhook).
2. Mints `mcp_review_token` via `domain/mcp_proxy.mint_token(review_id)`. No credential selection at this step — credential lookup is by `org_id` at dispatch time.
3. `plugins/claude_code` writes the workspace's `.mcp.json` with the proxy URL + bearer.
4. Reviewer continues normally; agents either get useful context from MCP tools or get `not_connected` errors and proceed without it.

Triggering identity flows through to audit rows: existing reviewer code sets `actor_kind = "user"` + `actor_user_id` when human-triggered, `actor_kind = "system"` for webhook. The proxy reads these from the review row at dispatch time and stamps the audit entry.

## Per-review token storage

- Insert: `token = secrets.token_urlsafe(32)`; persist `sha256(token)` only.
- Lookup: proxy receives `Authorization: Bearer <token>`, computes sha256, looks up row, checks `expires_at`.
- Same shape as M02 session tokens (32 random bytes, hashed in DB).
- Expiry: `review_started_at + max_review_duration + grace`. On review end, row deleted explicitly. Grace covers the case where the review process is still flushing its last MCP calls when the orchestrator decides the review is done.

## Settings UI shape

### Org Settings > Integrations

- Page renders a list of providers (M04: Linear, Notion). Each card shows:
  - Provider name + icon + docs link.
  - **Empty state**: copy that explicitly recommends creating a dedicated bot user in the upstream provider (e.g. `yaaos-bot@company.com`) and connecting as that account. "Connect" button kicks off the OAuth flow.
  - **Connected state**: `upstream_identity` (email / handle), `last_validated_at`, "Reconnect" and "Disconnect" buttons.
  - **Reconnect-required state**: red badge driven by `last_refresh_status = "failed"`. Owner clicks Reconnect to re-OAuth.
  - **Enabled toggle**: disabling preserves the credential row but stops the proxy from forwarding for this provider.
  - **Allowlist editor**: per-write-tool toggles for the provider's known write tools. Read tools are always permitted (default list defined in provider code).
  - **Test connection** button → calls `validate(...)`, updates `last_validated_at`.
- All mutations audit-logged.

## Risks

- **Refresh-lock contention under burst load.** If 20 webhook reviews fire at once and the org service-account access token has just expired, all 20 hit the lock. Only the first refreshes; the rest re-read the updated token. Acceptable but worth flagging — the lock is per-`(org_id, provider)` so a busy org won't block other orgs.
- **Connecting-as-yourself problem.** Owner OAuths and the integration uses their identity upstream; if they leave, integration breaks. Mitigated by docs + UI empty-state copy recommending a dedicated bot user. Not enforced.
- **`.mcp.json` write race.** If two reviews for the same workspace are running (shouldn't happen with current single-review-per-workspace model, but worth asserting), they'd stomp on each other's `.mcp.json`. M04 ships an assertion in `plugins/claude_code` that the workspace has no active review when materializing.
- **Advisory-lock-leak on crashed handler.** Postgres advisory locks are session-scoped — if the FastAPI process holding the lock crashes, the lock releases on connection close. Lock leak unlikely; still document the assumption.
- **MCP protocol drift.** Hosted MCP servers can break wire compatibility between versions. M04 wraps every JSON-RPC parse in a structured error path and audit-logs the failure rather than crashing the review.
- **Audit-log volume.** One row per JSON-RPC method means hundreds of rows per review at peak. The existing `audit_entries` retention (30 days, M02) is sufficient; mention in `core_audit_log.md` that MCP traffic is the largest contributor.
- **OAuth client registration.** yaaos needs OAuth clients registered with both Linear and Notion to obtain `client_id` + `client_secret`. Dev + prod each get their own. Both providers allow `localhost` callback URLs in dev. Operator step, captured in setup docs.
- **Provider-abstraction drift between Linear and Notion.** Implementing two providers in one milestone is intentional — it forces the abstraction to actually be provider-agnostic. Watch for "Linear-shaped" assumptions in `domain/integrations` that don't carry to Notion (refresh-token expiry semantics, callback parameters, scope set).

## Cross-references

- `apps/backend/docs/plugins_oauth_github.md` — outbound OAuth shape we're mirroring.
- `apps/backend/docs/core_audit_log.md` — `kind` value conventions (extended here).
- `plan/notes/mcp-context.md` — the source note. Deleted in Phase last after this milestone ships.
- `plan/notes/full-pr-flow.md` — reviewer module re-architecture; M04's review-token lifecycle plugs into the lifecycle described there.
- `plan/notes/security-posture.md` — workspace-egress trust boundary; Pattern B is what makes future containerized egress tractable.
