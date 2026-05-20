# M04 requirements

> Locked spec. Changes require explicit milestone amendment.

## What MCP gives the reviewer

Today the reviewer agent ships with only the PR diff. Half of "does this match intent" requires the *business* context — the Linear ticket the PR claims to close, the Notion design doc, the Sentry error that motivated the fix. MCP lets the agent fetch that context as part of the review run. M04 ships the foundation; downstream milestones add providers beyond Linear.

## Architecture choice

Pattern B (proxy-mediated). yaaos runs its own MCP server. The workspace's `.mcp.json` points the Claude Code CLI at yaaos. yaaos's proxy:

1. Authenticates the per-review bearer the workspace presents.
2. Resolves the workspace → review → attributed identity (user or org service account).
3. Forwards the JSON-RPC envelope to the upstream hosted MCP (`mcp.linear.app/sse`) using the attributed identity's OAuth bearer.
4. Streams the response back.
5. Writes an audit row per JSON-RPC method.

The workspace never holds the upstream OAuth token. This is the only design that makes containerized workspace egress firewalls tractable later: the proxy URL is the workspace's single allowed outbound destination.

## Credentials

- **One credential per `(org_id, provider)`.** No per-user credentials.
- Owner connects yaaos to Linear / Notion once for the org via OAuth in Org Settings > Integrations. The connecting OAuth identity becomes the org's service account for that provider.
- Recommended (not enforced) in setup docs: create a dedicated bot user in Linear/Notion (e.g. `yaaos-bot@company.com`) and connect as that, so the integration survives any single employee leaving the company.
- Every review — user-triggered or webhook-triggered — calls upstream as the org service account.
- If a provider isn't connected for the org, the proxy returns a structured `not_connected` error to the agent. Review still runs; agent acknowledges missing context and continues.

## Allowlists

- **Read tools default to "all known-safe read tools"** per provider, declared as a constant in each provider's code. Empty `allowed_tools` ⇒ all reads allowed.
- **Write tools are off by default.** Each write tool (e.g. Linear's `update_issue`, Notion's `update_page`) must be explicitly named in `mcp_credentials.allowed_tools text[]` for that org's credential row.
- One allowlist per `(org_id, provider)`. No org/user nesting — there's only one tier.

## Per-review tokens

- Issued at review start: `mcp_review_token = secrets.token_urlsafe(32)`.
- `mcp_review_tokens(token_hash, review_id, expires_at)` row inserted. `expires_at = created_at + 2 hours` — hard failsafe TTL.
- Workspace `.mcp.json` written with that token in the `Authorization` header for the proxy URL.
- Deleted on review end (success / fail / timeout / cancel).
- Proxy verifies `expires_at > now()` on every lookup. Expired tokens reject regardless of whether the explicit delete ran.
- Periodic cleanup sweep purges `mcp_review_tokens WHERE expires_at < now()` daily, in the same scheduler that handles M02's session cleanup. Catches orphans from crashed reviewers.
- Token is the workspace's only outbound capability for the review.

## Refresh-token rotation

Linear and Notion both rotate refresh tokens on use. Two concurrent reviews refreshing simultaneously would race: one gets the new pair, the other gets `invalid_grant` and the integration breaks.

Mitigation: per-`(org_id, provider)` Postgres advisory lock around any refresh. Inside the lock, re-read the row; if a concurrent refresh already updated the token, use it; else POST the refresh endpoint, persist, release.

Same discipline as GitHub installation-token refresh already in the codebase.

## Broken-credential detection and loud failure

If the upstream OAuth grant is revoked or expires (employee who connected leaves; admin rotates secrets; scope change invalidates the grant), reviews would silently degrade to `not_connected`-style errors and no one would notice. Six layers of defense against silent degradation:

### Distinct error codes

The proxy distinguishes three failure modes in `result_summary`:

- `not_connected` — org never connected this provider, or it's disabled. Expected.
- `broken_creds` — credential row exists and is enabled, but `last_refresh_status = "failed"`. **Not** expected; loud alert.
- `blocked_by_allowlist` — tool isn't in the org's `allowed_tools`.

The structured JSON-RPC error returned to the agent uses the same codes. Agent prompts include a line: "if a tool returns `broken_creds`, mention prominently in your review that the integration needs reconnection."

### Detection on refresh failure

Any refresh that returns `invalid_grant` / 401 / 403:

- Sets `last_refresh_status = "failed"`.
- Sets `last_refresh_failed_at = now()`.
- Emits `mcp.<provider>.token_refresh_failed` audit entry.

A successful refresh or reconnect flips the status back to `"ok"` and clears `last_refresh_failed_at`.

### Scheduled health-check

A new periodic job runs every hour and calls `validate(org_id, provider)` on every enabled credential row. Catches breakage between reviews. Updates `last_validated_at` on success; flips status to `"failed"` on failure.

### In-app banner

When any provider for the org has `last_refresh_status = "failed"`:

- Red banner appears in the app shell on every page, visible to Owners and Admins only (Members can't fix it).
- Banner text: "<Provider> integration needs to be reconnected." Click → `/orgs/{slug}/settings/integrations`.
- Org Settings > Integrations badges the broken provider in red.
- Coding Agents > Claude Code settings page shows a warning at the top whenever an enabled MCP provider is broken, since that's where users land when thinking about review quality.

### Email notification

When a provider transitions to `failed`:

- Email all Owners. Subject: `[yaaos] {provider} integration disconnected — action required`. Body: 3 lines + deep link to Settings > Integrations.
- Dedup via `last_failure_notified_at` column: re-notify only if 24h has passed and state is still broken.
- Reuses M02's invitation-email infrastructure (SMTP via Mailpit in dev, real SMTP in prod).

### Loud warning on every review output

If any enabled provider for the org is broken during a review, the review output (the PR comment yaaos posts to GitHub) starts with a yellow warning block:

> ⚠️ **{Provider} integration is disconnected.** This review ran without {provider} context. Reconnect at Settings > Integrations.

This surface is unavoidable — even if Owners ignore the in-app banner and miss the email, every PR review touches this.

## Audit

One row per inbound JSON-RPC method. Uses existing `core/audit_log`:

- `actor_kind`: reflects who triggered the review — `user` for user-triggered reviews, `system` for webhook-triggered reviews.
- `actor_user_id`: set when `actor_kind = user`.
- `entity_kind = "review"`, `entity_id = review_id`.
- `kind = "mcp.<server>.<method>"` (e.g. `mcp.linear.tools/call`).
- `payload`: `{server, method, tool_name?, args_hash, result_summary, upstream_latency_ms, upstream_account: "org_service_account"}`. The triggering identity (in `actor_*`) and the upstream identity (in `payload.upstream_account`) are separately legible.

`args_hash` is sha256 of arguments. Raw args may contain customer data (ticket contents, issue descriptions). Admin can later opt-in to raw-args retention behind an explicit setting; default is hash-only.

Additional audit kinds: `mcp.<provider>.token_refreshed`, `mcp.<provider>.token_refresh_failed`. Surfaces revoked/expired service-account state without polling.

Retention: M04 lowers `AUDIT_LOG_RETENTION` from 30 days (M02) to **15 days**. Applies to all audit categories — MCP traffic dominates volume, and 15d retains enough forensic depth across all event types.

## Settings UI

### Org Settings > Integrations (Owner / Admin only)

- List of providers (M04: Linear, Notion) with status badge ("Connected" / "Disconnected" / "Reconnect required").
- Per-provider:
  - Empty state: explicitly recommends creating a dedicated bot user in the upstream provider and connecting as that account. "Connect Linear" / "Connect Notion" button runs the OAuth flow.
  - Connected state: shows the connected upstream identity (email / handle), `last_validated_at`, "Reconnect" and "Disconnect" buttons.
  - "Reconnect required" state: red badge when refresh fails. Owner re-OAuths to fix.
  - Enable / disable toggle (disabling preserves credentials but stops the proxy from forwarding for this provider).
  - Allowlist editor: read tools always on; per-write-tool toggles for the provider's known write tools (off by default).
  - "Test connection" button — runs a minimal upstream call to validate.

## Top-level nav (additions to M03's sidebar)

- Org Settings > Integrations (between BYOK and Audit).
- No User popover additions. (Per-user credentials are out of scope.)

## Permissions summary

| Page | View | Edit |
|---|---|---|
| Org Settings > Integrations | Owner, Admin | Owner, Admin |

## Data model additions

- `mcp_credentials` — PK `(org_id, provider)`. Columns: `encrypted_access_token`, `encrypted_refresh_token`, `expires_at`, `scopes text[]`, `allowed_tools text[] default '{}'`, `enabled bool default true`, `upstream_identity text` (the email / handle the OAuth flow returned, for display), `last_validated_at`, `last_used_at`, `last_refresh_status text` (`"ok"` / `"failed"`), `last_refresh_failed_at timestamptz`, `last_failure_notified_at timestamptz`, `created_at`, `updated_at`. Encrypted at rest with M02's master key.
- `mcp_review_tokens` — PK `token_hash`. Columns: `review_id`, `expires_at`, `created_at`. Token's raw value never persisted; stored as sha256 hex.
- Audit additions: new `kind` values (`mcp.*`). No new `actor_kind`: the existing `user` / `system` values cover triggering identities.

## Endpoints

### Outbound OAuth (per provider, M04 ships Linear + Notion)

- `GET /api/orgs/{slug}/integrations/{provider}/connect` — start OAuth flow. Signed `state` carries `(org_id, user_initiating)`.
- `GET /api/integrations/{provider}/callback` — exchange code, persist tokens against `(org_id, provider)`.
- `DELETE /api/orgs/{slug}/integrations/{provider}` — clear credentials.
- `POST /api/orgs/{slug}/integrations/{provider}/validate` — minimal upstream call to test connectivity.

### MCP proxy

- `POST /api/mcp/{review_id}/{server}` — MCP Streamable HTTP endpoint. Accepts JSON-RPC POSTs and supports SSE upgrade. Authenticates via `Authorization: Bearer <mcp_review_token>`.

### Settings

- `GET /api/orgs/{slug}/integrations` — list providers with status, upstream identity, allowlist, timestamps.
- `PATCH /api/orgs/{slug}/integrations/{provider}` — enable/disable, update allowlist.

All mutations audit-logged with appropriate `kind`.

## Cross-cutting test requirements

- Per-`(org_id, provider)` lock test: two concurrent refresh attempts; one performs the refresh, the other observes the updated row.
- Unconnected-provider flow: agent's `tools/call` against a non-connected provider → proxy returns structured `not_connected` error.
- Allowlist enforcement: `tools/call` for a write tool not in `allowed_tools` → rejected, audit row written with `result_summary = "blocked_by_allowlist"`.
- Per-review token lifecycle: token works during review, deleted on review end; subsequent calls with the token return 401. URL-path mismatch (token from review A used on URL for review B) → 401.
- TTL failsafe: token whose row is never explicitly deleted but whose `expires_at` has passed → proxy returns 401; periodic sweep removes the row.
- Refresh failure surfaces: simulate Linear refresh returning `invalid_grant`, observe `last_refresh_status = "failed"`, audit entry `mcp.linear.token_refresh_failed`, email queued to Owners, in-app banner appears, review output includes the yellow warning block.
- Re-notification dedup: a second refresh failure within 24h does not send another email; one after 25h does.
- Scheduled health-check: connected provider where upstream returns 401 → next 6h tick flips `last_refresh_status` to `"failed"` proactively.
- E2E: Owner connects Linear and Notion → reviews trigger MCP calls to both providers → audit shows org_service_account in payload and the triggering identity (user or system) in actor.

## Explicit cuts (POC scope of M04)

- Per-user credentials. Single org service account per provider.
- Providers beyond Linear + Notion.
- REST-shim path.
- Per-repo provider overrides.
- Raw-args retention in audit rows.
- PAT paste alternative to OAuth.
- Containerized workspace egress firewall (separate future milestone for workspace isolation).
- Usage / cost dashboards for MCP calls.
