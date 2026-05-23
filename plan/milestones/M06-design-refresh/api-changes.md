# M06 — REST API changes

> Concrete, per-surface inventory of backend REST API work for M06. Drives Phase 2+ implementation. Read alongside [requirements.md](requirements.md).

## How this doc is structured

1. **Summary** — total counts (new / delete / rename / extend) and pre-implementation cleanups.
2. **Per-surface table** — for each of the 19 M06 surfaces, the endpoints it touches and their status (`exists` / `extend` / `new` / `delete` / `rename`).
3. **Renames** — full route + symbol map for the M03→M06 vocabulary shift.
4. **New endpoints** — full sketches (method, path, params, request, response, auth) for every endpoint that doesn't exist today.
5. **Modified endpoints** — extensions to existing endpoints (new params, response field additions/removals).
6. **Deletions** — endpoints removed entirely.
7. **Cross-cutting concerns** — role rename, org-scoping, ticket-status vocabulary, builder-identity convention.
8. **Open questions** — anything left to resolve before Phase 3.

Every entry references `apps/backend/app/...` paths so implementers don't have to grep.

---

## Summary

| Category | Count |
|---|---|
| **New endpoints** | 17 |
| **Renamed endpoints / routes** | 12 (3 SPA route prefixes + 9 API prefixes/paths) |
| **Extended endpoints** (new params, richer response, or org-scoped) | 14 |
| **Deleted endpoints** | 0 (legacy `/api/settings/onboarding` is repurposed, not deleted) |
| **Schema renames** (role `member`→`builder`) | 1, cascading through ~10 endpoints |

Implementation tracks **two pre-anchor backend chores** that need to land before any Tier-1 anchor work:

- **Backend chore A — `member`→`builder` role rename.** Cascades through `MembershipRole`, action constants, membership endpoint responses, and SPA types. Touches `apps/backend/app/domain/orgs/` (memberships, audit, permissions), `apps/backend/app/domain/identity/`, and ~10 API responses. Migration writes a single `UPDATE org_memberships SET role='builder' WHERE role='member'` plus an enum migration.
- **Backend chore B — org-scope the M01-era routers.** `apps/backend/app/domain/tickets/web.py`, `apps/backend/app/domain/memory/web.py`, `apps/backend/app/domain/reviewer/web.py` all hard-code `M01_ORG_ID`. Replace with `X-Org-Slug` resolution + `require(Action.*)` gates the same way M03 modules do. Touches every endpoint in those three routers (15 endpoints total).

These two chores are infrastructure for the rest of M06. They land in **F1 Phase 2** alongside chrome composites.

---

## Per-surface table

Each surface lists every endpoint it depends on. Status tag legend: `exists` (use as-is) · `extend` (add params or response fields) · `new` (must build) · `rename` (path/prefix change only) · `delete` (remove).

### Login (`/login`)

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/auth/me` | exists | Auth check; route guard. |
| GET | `/api/auth/providers` | exists | Provider list. |
| GET | `/api/auth/sso/discover?email=…` | **new** | Returns the SSO IdP (if any) matching the email's domain — drives "Continue with [IdP]" button vs "Continue with GitHub" per E2a.18. |
| GET | `/api/auth/login?provider=…&next=…` | exists | Start OAuth. |
| POST | `/api/auth/totp/challenge` | exists | 2FA step-up post-OAuth. |

### Org picker (`/orgs`)

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/orgs/mine` | **new** | Returns the user's orgs with the fields the picker needs per E2a.19: `[{id, slug, name, role, last_used_at}]`. **Replaces** `auth/me.orgs[]` for picker use; `auth/me.orgs[]` stays as a thinner "what orgs am I in" list for routing. |
| POST | `/api/orgs` | **new** | Create org. Request: `{name, slug}`. Response: full Org. Per C1 "Create new organization" modal. Caller becomes Admin. |

### Org switcher (sidebar chip)

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/orgs/mine` | **new** (shared with Org picker) | Same endpoint. Switcher uses `name + slug`, skips `last_used_at`. |

### Dashboard (`/orgs/:slug/dashboard`)

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/orgs/config-status` | **new** | Aggregated readiness check for the "not configured" gate per B3. Response: `{configured: bool, missing: ["vcs" | "coding_agent" | "api_key" | "workspace_provider"], admins: [{user_id, display_name, primary_email}]}`. Replaces the SPA's current piecewise check (`/api/settings/onboarding` + `/api/byok` + …). |
| GET | `/api/tickets/dashboard` | **new** | Single-query Dashboard projection per E2a.3. Response: `{stats: {in_flight, hitl_pending, completed_today, failed_today}, in_flight: [TicketRow…5–10], needs_attention: [TicketRow…3–5]}`. Dedicated endpoint instead of multiple `/api/tickets?status=…` calls because Dashboard repeats every few seconds and benefits from one round-trip + server-side counting. |
| SSE | `/api/events?kinds=ticket_status_changed,workflow_state_changed` | exists | Auto-invalidates `/api/tickets/dashboard` on relevant events. |

The `/api/settings/onboarding` endpoint is **repurposed/replaced** by `/api/orgs/config-status` — see Deletions. The legacy onboarding endpoint can stay during F1 Phase 2 → Phase 5 transition and be deleted in F1 Phase 9 cleanup.

### Tickets list (`/orgs/:slug/tickets`)

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/tickets` | **extend** | See [Modified endpoints — `/api/tickets`](#tickets-list-1) below. Adds: multi-status filter using M06's display vocabulary (running/hitl/done/failed/cancelled), repo multi-select, builder filter, date range, free-text search over title, `sort` param, cursor pagination. Response gains `findings_count`, `max_severity`, `current_stage`, `builder` (replaces/supplements `author_login`). Org-scoping replaces `M01_ORG_ID`. |
| GET | `/api/github/repositories` | exists | Repo filter dropdown source. |
| GET | `/api/memberships` | exists | Builder filter dropdown source. |

### Ticket detail (`/orgs/:slug/tickets/:ticketId`)

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/tickets/:ticket_id` | **extend** | Add `stages: [{name, state, attempt_count, current_attempt, started_at, completed_at}]` to the response so the stage indicator (E2a.4) and "Re-run [Stage name]" button can render without a separate fetch. Add `builder: {kind: "user"\|"system", user_id?, display_name, avatar_url?}` per the system-trigger convention. |
| GET | `/api/reviewer/findings/by-ticket/:ticket_id` | **extend** | Add per-finding `state` transitions to support inline Ack / Push back (M06's UI removes the separate Conversations tab). Currently returns enough state for badges; verify `acked` and `pushed_back` are in the enum. |
| POST | `/api/reviewer/findings/:finding_id/ack` | **new** | Ack a finding. Request: `{}`. Response: updated Finding. Replaces the current implicit ack inside the thread. |
| POST | `/api/reviewer/findings/:finding_id/push-back` | **new** | Push back on a finding with a reason. Request: `{reason: str}`. Response: updated Finding. |
| GET | `/api/reviewer/findings/:finding_id/thread` | rename | Currently `/api/reviewer/threads/by-finding/:finding_id`. Renamed for REST consistency; same shape. |
| POST | `/api/tickets/:ticket_id/hitl/respond` | **new** | Submit a HITL response. Request: shape depends on prompt — backend passes through to `core.workflow.resume_hitl`. Response: `{stage: …, next_state: …}`. Currently only the service-layer function exists; no HTTP. |
| GET | `/api/tickets/:ticket_id/hitl/history` | **new** | List past HITL exchanges (prompt + response + timestamps) per E2a.4 HITL tab "History" subsection. |
| POST | `/api/reviewer/rereview` | exists | "Re-run Review" action. Cost-protective confirm in SPA. |
| POST | `/api/reviewer/cancel?ticket_id=…` | exists | "Cancel Review" action. Destructive confirm in SPA. |
| GET | `/api/reviewer/reviews/by-ticket/:ticket_id` | exists | Per-review timeline if shown inside Activity tab. |
| SSE | `/api/workspaces/workflows/:workflow_execution_id/activity` | exists | Live activity stream for the Activity tab. |
| SSE | `/api/events?ticket_id=…&kinds=ticket_status_changed,workflow_state_changed,finding_*,hitl_*` | exists | Invalidations for everything else. **Verify** `hitl_*` kinds emit when a HITL prompt arrives/resolves — see Open questions. |

Endpoints **dropped** from Ticket detail by the redesign:

- `/api/reviewer/jobs/by-ticket/:ticket_id` — the M03 "agent card" view is replaced by the stage indicator. The endpoint stays available (other consumers may exist in dev tooling) but the SPA stops calling it.
- `/api/reviewer/conversations/by-ticket/:ticket_id` — same; the Conversations tab is removed in M06. Endpoint stays available but unused by SPA.

(These two stay in the codebase rather than being deleted because they read from `workflow_executions` views — keeping them is zero-cost, and removing them would be a backend chore not justified by the SPA redesign alone.)

### Lessons (`/orgs/:slug/lessons`) — formerly Memory

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/lessons` | **rename + extend** | Was `/api/memory`. Adds: `q` (free-text search over title), `repo_external_ids` (multi-select; current is single), `created_by` (Builder filter), `created_after`/`created_before` (date range), `sort`, cursor pagination per E2a.5. Org-scoping replaces `M01_ORG_ID`. |
| POST | `/api/lessons` | rename | Was `POST /api/memory`. Body unchanged (`{repo_external_id, title, body, source_pr_url, plugin_id}`). |
| PUT | `/api/lessons/:id` | rename | Was `PUT /api/memory/:id`. |
| DELETE | `/api/lessons/:id` | rename | Was `DELETE /api/memory/:id`. |
| GET | `/api/lessons/:id` | **new** | The current router has no GET-one. Inline-expand on row click per E2a.5 fetches full body via this endpoint. |
| — | **Schema change to `Lesson`** | **extend** | Add `created_by: {user_id, display_name}` — currently `Lesson` has no actor; row already exists in DB via audit but not denormalized on the lesson row. Either add an `actor_user_id` column to `lessons` (preferred — simple FK) or join through audit. Decide in implementation. |

### Notifications (`/notifications` + popover) — **fully new feature**

There is no notifications module today. Everything below is new.

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/notifications` | **new** | List notifications for current user, cross-org. Query: `read_state` (all/unread/read), `org_id` (single), `types` (multi), `before` (cursor), `limit`. Response: `[Notification…]` per the row shape in E2a.6, grouped client-side. Cross-org → no `X-Org-Slug` required; auth from session. |
| POST | `/api/notifications/:id/read` | **new** | Mark one as read. Idempotent. Response: updated Notification. |
| POST | `/api/notifications/mark-read` | **new** | Mark all matching the current filter as read. Request: `{read_state?, org_id?, types?}` mirroring the GET filter. Response: `{marked: int}`. |
| GET | `/api/notifications/popover` | **new** | Trimmed peek — latest N unread for the sidebar bell per E2a.7. Returns `{items: [...max 10], unread_count: int}`. Single round-trip avoids the popover needing the GET-list-and-count separately. |
| SSE | `/api/events?kinds=notification_*` | **new** | Live unread-count badge. Two new event kinds: `notification_created`, `notification_read`. SPA invalidates `/api/notifications/popover` and (if open) `/api/notifications`. |
| — | **Backend module** | **new** | See Notifications module sketch below. |

#### Notifications module sketch (greenfield)

The largest piece of new backend work in M06. Lives at `apps/backend/app/domain/notifications/`.

**Files:**

- `models.py` — `NotificationRow` SQLAlchemy model.
- `service.py` — write paths (`record_*`), read paths (`list_for_user`, `mark_read`, `mark_all_read`), event-subscription wiring.
- `web.py` — the 4 endpoints listed above (`/api/notifications`, `/api/notifications/:id/read`, `/api/notifications/mark-read`, `/api/notifications/popover`).
- `subscribers.py` — wires `core/events` subscriptions to write notifications on workflow transitions.

**Schema** (one table):

```
notifications (
  id            UUID primary key,
  user_id       UUID not null  references users(id),         # recipient
  org_id        UUID not null  references orgs(id),          # source org for the row's "org" field
  type          text not null,                                # "hitl_waiting" | "ticket_completed" | "ticket_failed"
  ticket_id     UUID null      references tickets(id),       # source ticket (nullable for non-ticket future types)
  title         text not null,                                # one-line headline rendered as-is in the SPA
  body          text not null,                                # one-line description rendered as-is
  read_at       timestamptz null,
  created_at    timestamptz not null default now()
)
index (user_id, read_at, created_at desc)                     # primary read path: "unread + recent for me"
index (user_id, org_id, type, created_at desc)                # filter combinations
```

**Subscription wiring** (in `subscribers.py`):

- Subscribe to `core/events` kind `workflow_state_changed`.
- On transition to `awaiting_human`: write a `hitl_waiting` notification for `Ticket.builder.user_id`.
- On transition to `done`: write a `ticket_completed` notification.
- On transition to `failed`: write a `ticket_failed` notification.
- Apply the [fan-out rule from E2a.6](requirements.md): skip when `builder.kind === "system"` or `builder.user_id is null`.
- Emit `notification_created` event to `core/events` so the SPA's SSE invalidates.

**Retention:** none in M06. Notifications accumulate. Add a TTL sweep in a future milestone if rows grow unwieldy.

**Idempotency:** writes are deduplicated by `(user_id, type, ticket_id)` — re-emitting the same transition (e.g., engine retries) does not create a second notification. Mark-read is idempotent by definition (writes `read_at = now()` if null).

### Settings — Auth (`/orgs/:slug/settings/auth`)

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/sso/config` | exists | |
| PUT | `/api/sso/config` | exists | |
| GET | `/api/orgs` | exists | Session timeout lives here. |
| PATCH | `/api/orgs` | exists | |

### Settings — Members (`/orgs/:slug/settings/members`)

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/memberships` | **extend** | Response field `role`: `"admin" \| "builder"` (was `"admin" \| "member"`) — see [role rename](#role-rename). |
| POST | `/api/memberships/invite` | **extend** | Request body `role`: same enum change. |
| PATCH | `/api/memberships/:user_id` | **extend** | Same enum change. |
| DELETE | `/api/memberships/:user_id` | exists | |
| POST | `/api/memberships/accept` | exists | |

### Settings — Audit (`/orgs/:slug/settings/audit`)

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/audit` | exists | Filters and shape already match E2a.10. |

### Settings — VCS (`/orgs/:slug/settings/vcs` + detail)

The current `/api/vcs` is single-plugin (one VCS install per org). M06 designs the SPA as a list + detail for forward-compat with multi-VCS, but **for M06 the GitHub-only list is just a list-of-1** — no backend change required beyond the SPA reading `/api/vcs` and rendering it as a one-item list. Future multi-VCS expansion is a separate milestone.

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/vcs` | exists | |
| POST | `/api/vcs` | exists | |
| DELETE | `/api/vcs` | exists | |
| GET | `/api/plugins/available?type=vcs` | exists | Picker modal source. |
| GET | `/api/github/installation` | exists | Status card on detail page. |
| POST | `/api/github/credentials` | exists | |
| GET | `/api/github/repositories` | exists | Repo picker in detail. |

### Settings — Coding Agents list + detail

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/coding-agents` | exists | List page. |
| POST | `/api/coding-agents` | exists | Picker modal → install. |
| PATCH | `/api/coding-agents/:plugin_id` | **extend** | Detail page settings shape per E2a.2 (orchestrator + sub-agents + MCP context). Today `settings` is opaque `dict` — verify the Claude Code plugin schema supports the M06 shape (`orchestrator.{model, effort, use_default_system_prompt, system_prompt}`, `sub_agents: […≤8]`, `mcp_proxy_ids: […]`). Likely needs a small extension in `apps/backend/app/plugins/claude_code/` to model sub-agents explicitly. |
| DELETE | `/api/coding-agents/:plugin_id` | exists | Uninstall (danger zone). |
| GET | `/api/plugins/available?type=coding_agent` | exists | |
| GET | `/api/claude_code/defaults` | exists | Powers default-toggle + dropdown enums on detail page. |
| POST | `/api/claude_code/api_key` | exists | The "Anthropic API key" field on the detail page header. Already shares state with `/api/byok` — keep both routes; the SPA will write to the canonical one and the other stays for compat. |

### Settings — Workspace (`/orgs/:slug/settings/workspace`)

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/orgs` | exists | `workspace_provider`, `registered_iam_arn`. |
| PATCH | `/api/orgs` | exists | |
| GET | `/api/workspaces/connection_status` | exists | Heartbeat banner (when provider = remote_agent). |

### Settings — MCP Proxy (`/orgs/:slug/settings/mcp-proxy`) — renamed from Integrations

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/mcp-proxy` | **rename** | Was `/api/integrations`. |
| GET | `/api/mcp-proxy/:provider/connect` | rename | |
| GET | `/api/mcp-proxy/:provider/callback` | rename | OAuth callback. **Coordinate** with each provider's OAuth app's registered redirect URI — see [Renames](#renames). |
| POST | `/api/mcp-proxy/:provider/validate` | rename | |
| PATCH | `/api/mcp-proxy/:provider` | rename | |
| DELETE | `/api/mcp-proxy/:provider` | rename | |

The forwarding endpoint `/api/mcp/:review_id/:server` (the JSON-RPC proxy used by coding agents) is a **separate concern from the settings surface** and **keeps its current `/api/mcp` prefix**. Renaming only the org-settings management endpoints; the agent-facing forwarding stays put because its URLs are stored in agent runtime contexts.

### Settings — API Keys (`/orgs/:slug/settings/api-keys`) — renamed from BYOK

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/api-keys` | **rename** | Was `/api/byok`. |
| POST | `/api/api-keys/:provider` | rename | |
| POST | `/api/api-keys/:provider/validate` | rename | |
| DELETE | `/api/api-keys/:provider` | rename | |

### User — Details (`/user/details`)

| Method | Path | Status | Notes |
|---|---|---|---|
| GET | `/api/account/me` | exists | |
| PATCH | `/api/account/me` | exists | Display name; clear GitHub. |
| GET | `/api/account/emails` | exists | |
| POST | `/api/account/emails` | exists | |
| DELETE | `/api/account/emails/:email_id` | exists | |
| PATCH | `/api/memberships/me/:org_id` | exists | Per-org handle. |
| GET | `/api/account/github/verify` | exists | Verify-only OAuth start. |
| GET | `/api/account/github/verify/callback` | exists | |

### User — Security (`/user/security`)

| Method | Path | Status | Notes |
|---|---|---|---|
| POST | `/api/auth/logout-all` | exists | "Sign out of all sessions". |
| POST | `/api/auth/totp/enroll` | exists | |
| POST | `/api/auth/totp/verify` | exists | |
| GET | `/api/account/totp/recovery-codes` | **new** | Per E2a.16 "recovery codes when enrolled". Returns one-time recovery codes after enrollment. The TOTP-enroll endpoint currently doesn't return recovery codes; the security page should display them. **Verify** whether recovery codes are part of TOTP today — see Open questions. |

### User — Messaging (`/user/messaging`)

Route placeholder per E2a.17. No endpoints in M06. Empty-state copy only.

---

## Renames

### SPA route renames

| Old SPA route | New SPA route |
|---|---|
| `/orgs/:slug/memory` | `/orgs/:slug/lessons` |
| `/orgs/:slug/settings/integrations` | `/orgs/:slug/settings/mcp-proxy` |
| `/orgs/:slug/settings/byok` | `/orgs/:slug/settings/api-keys` |
| `/account/*` | `/user/*` |

### API path renames

| Old API path | New API path | Backend module |
|---|---|---|
| `/api/memory` (+ children) | `/api/lessons` (+ children) | `app/domain/memory/` → rename module to `app/domain/lessons/` |
| `/api/integrations` (+ children) | `/api/mcp-proxy` (+ children) | `app/domain/integrations/` → rename module to `app/domain/mcp_proxy_settings/` (note: `app/domain/mcp_proxy/` already exists as the JSON-RPC forwarder — pick a distinct module name to avoid collision) |
| `/api/byok` (+ children) | `/api/api-keys` (+ children) | `app/domain/orgs/byok_routes.py` → `app/domain/orgs/api_keys_routes.py` |
| `/api/reviewer/threads/by-finding/:id` | `/api/reviewer/findings/:id/thread` | `app/domain/reviewer/web.py` — REST-consistency only |

### Backend symbol renames (cascade from API renames)

- `memory` package → `lessons` package. Schema name `Lesson` already matches.
- `integrations` package → `mcp_proxy_settings` (or similar). `IntegrationStatus` class → `McpProxyStatus`.
- `byok_routes.py` → `api_keys_routes.py`. `ByokProvider*` types → `ApiKey*`.
- `MembershipRole` enum: `member` → `builder`. Action constants stay (`MEMBERS_READ` etc.) since they're not role-named.
- `actor_kind` audit values for role changes adjust to `builder`.

### OAuth callback URL coordination

The `/api/mcp-proxy/:provider/callback` rename **changes registered OAuth redirect URIs** for Linear / Notion. Either:

1. Land the rename with a redirect handler at the old path that forwards to the new path (low-risk; reversible), **or**
2. Update the OAuth app registrations as part of the rename PR.

Decide in F1 Phase 2 implementation; (1) is the safer default.

---

## New endpoints — full sketches

### `GET /api/auth/sso/discover`

- **Auth:** `public_route`. No session required (called before login).
- **Query:** `email: str` (required).
- **Response:** `{provider: "github" | "saml", saml_org_slug?: str, saml_idp_name?: str}` — when domain matches a configured SSO IdP, return SAML; otherwise fall back to GitHub.
- **Rate limit:** `AUTH_LIMIT`.
- **Why:** Drives the Login page's provider-button rendering per E2a.18.

### `GET /api/orgs/mine`

- **Auth:** session cookie required; no `X-Org-Slug` (cross-org).
- **Response:** `[{id: UUID, slug: str, name: str, role: "admin"|"builder", last_used_at: datetime|null}]`.
- **`last_used_at`** comes from sessions table or a new `last_used_at` column on `org_memberships` (decide in impl — likely the latter; updated whenever the user navigates into an org).
- **Why:** Powers Org picker + Org switcher.

### `POST /api/orgs`

- **Auth:** session cookie required; no `X-Org-Slug`.
- **Request:** `{name: str, slug: str}`.
- **Response:** `{id, slug, name, role: "admin"}` — caller becomes Admin of the new org.
- **Failures:** `409 slug_taken`, `422 invalid_slug`.
- **Rate limit:** `MUTATE_LIMIT`.

### `GET /api/orgs/config-status`

- **Auth:** `ORG_READ` (any member).
- **Response:**
  ```
  {
    configured: bool,
    missing: ("vcs" | "coding_agent" | "api_key" | "workspace_provider")[],
    admins: [{user_id, display_name, primary_email}]
  }
  ```
- `missing` is empty iff `configured` is true. `admins` populated for Builder copy ("ask [admin] to finish setup").
- **Why:** The "not configured" gate per B3 + Dashboard banner per E2a.3.

### `GET /api/tickets/dashboard`

- **Auth:** `TICKETS_READ` (any member; new action constant — currently tickets routes use `public_route`).
- **Response:**
  ```
  {
    stats: {in_flight: int, hitl_pending: int, completed_today: int, failed_today: int},
    in_flight: [TicketRow…5–10],
    needs_attention: [TicketRow…3–5]
  }
  ```
- `TicketRow` is the same shape returned by extended `/api/tickets` (see below) — single canonical row shape.
- **Why:** Dashboard one-round-trip projection per E2a.3.

### `POST /api/reviewer/findings/:finding_id/ack`

- **Auth:** `TICKETS_READ` (all Builders/Admins per A1).
- **Request:** `{}` (empty body; idempotent).
- **Response:** Updated `Finding` with `state: "acked"`.
- **Audit:** `finding.acked` entry.

### `POST /api/reviewer/findings/:finding_id/push-back`

- **Auth:** same as ack.
- **Request:** `{reason: str}` (≥10 chars; UI enforces).
- **Response:** Updated `Finding` with `state: "pushed_back"`.
- **Audit:** `finding.pushed_back` entry with reason in payload.

### `POST /api/tickets/:ticket_id/hitl/respond`

- **Auth:** `TICKETS_READ`.
- **Request:** prompt-shape-specific JSON (passthrough to `core.workflow.resume_hitl`).
- **Response:** `{stage: str, next_state: WorkflowState}`.
- **Failures:** `404 ticket_not_found`, `409 no_pending_hitl` (if no current pause), `422 invalid_response`.
- **Why:** Service-layer function `resume_hitl` exists at `apps/backend/app/core/workflow/service.py:560` but has no HTTP wrapper — this adds the wrapper.

### `GET /api/tickets/:ticket_id/hitl/history`

- **Auth:** `TICKETS_READ`.
- **Response:** `[{stage, attempt, prompt: object, response: object|null, prompted_at, resolved_at, resolver: {user_id, display_name}|null}]`, chronological.
- **Source:** `pending_human_decisions` table — already exists per `apps/backend/app/core/workflow/models.py:53`.

### `GET /api/lessons/:id`

- **Auth:** `LESSONS_READ` (new action constant; currently memory is `public_route`).
- **Response:** full `Lesson` (with new `created_by` field).
- **Why:** Inline-expand on the lessons list per E2a.5.

### `GET /api/notifications`

- **Auth:** session cookie required; no `X-Org-Slug` (cross-org).
- **Query:** `read_state` (all/unread/read; default unread), `org_id` (optional), `types` (multi, optional), `before` (cursor; ISO timestamp), `limit` (default 50, max 200).
- **Response:** `[{id, type, org: {id, slug, name}, ticket_id?, title, body, read: bool, created_at}]`.
- **Why:** Notifications full page per E2a.6.

### `POST /api/notifications/:id/read`

- **Auth:** session.
- **Request:** `{}`.
- **Response:** updated Notification (`read: true`).
- **Idempotent.**

### `POST /api/notifications/mark-read`

- **Auth:** session.
- **Request:** `{read_state?, org_id?, types?}` (mirror filter).
- **Response:** `{marked: int}`.

### `GET /api/notifications/popover`

- **Auth:** session.
- **Response:** `{items: [Notification…max 10], unread_count: int}`.
- **Why:** Sidebar bell popover per E2a.7. Lighter than `/api/notifications`.

### `GET /api/account/totp/recovery-codes`

- **Auth:** session + TOTP enrolled.
- **Response:** `{codes: [str…10]}` — one-time recovery codes.
- **Failures:** `409 totp_not_enrolled`.
- **Why:** E2a.16 "recovery codes when enrolled". **Verify** existing TOTP flow first — see Open questions.

### `GET /api/events?kinds=notification_*` (event-kinds extension, not a new endpoint)

- **What's new:** the `notification_created` + `notification_read` event kinds emitted by the new notifications module. The `/api/events` endpoint itself already exists.
- **Why:** Live unread-count badge.

---

## Modified endpoints

### `/api/tickets` — Tickets list

**Before** (today):

```
GET /api/tickets
  query: repo_external_id: list[str]?, status: list[str]?, limit: int=50
  response: [Ticket]
  org-scoping: hard-coded M01_ORG_ID
```

**After** (M06):

```
GET /api/tickets
  auth: TICKETS_READ (X-Org-Slug → org_id)
  query:
    status: list[Literal["running","hitl","done","failed","cancelled"]]?  # M06 vocab
    repo_external_id: list[str]?
    builder_user_id: list[UUID]?                       # NEW
    builder_kind: Literal["user","system"]?            # NEW — system = yaaos-triggered
    created_after: datetime?                           # NEW
    created_before: datetime?                          # NEW
    q: str?                                            # NEW — free-text over title
    sort: Literal["updated_desc","updated_asc","created_desc","status","findings_count"]?
    cursor: str?                                       # NEW — opaque pagination cursor
    limit: int = 50, max 200
  response:
    {items: [TicketRow], next_cursor: str | null}
```

`TicketRow`:

```
{
  id, title, status,                                   # M06 status vocab
  repo_external_id, repo_html_url,
  current_stage: str | null,                           # NEW — e.g. "Review"
  stage_attempt_count: int,                            # NEW
  findings_count: int,                                 # NEW — denormalized
  max_severity: "low"|"medium"|"high" | null,          # NEW
  builder: {kind, user_id?, display_name, avatar_url?},# NEW (replaces author_login as primary)
  author_login: str | null,                            # kept for compat; sourced from PR
  pr_number: int | null,
  pr_html_url: str | null,
  created_at, updated_at
}
```

**Backend work:** add `findings_count` / `max_severity` denormalization on `tickets` (either as columns updated by finding-write triggers, or computed on read with a join — POC-acceptable). Map workflow state → M06 status vocab in the same projection used by Dashboard.

### `/api/tickets/:ticket_id` — Ticket detail header

**Adds:** the same `current_stage`, `builder`, `findings_count`, `max_severity` fields as the list row, **plus** `stages: [{name, state, attempt_count, current_attempt, started_at, completed_at, workflow_execution_id}]` for the stage indicator.

`workflow_execution_id` is the input to `/api/workspaces/workflows/:workflow_execution_id/activity` (existing SSE).

### `/api/reviewer/findings/by-ticket/:ticket_id` — Findings list

- **Adds:** filter `state: list[Literal["open","acked","pushed_back"]]?` (defaults to `["open","acked","pushed_back"]` — i.e. all non-terminal).
- **Verifies:** the finding `state` field already includes `acked` / `pushed_back` values produced by the new ack/push-back endpoints.

### `/api/memberships` — Members list

- **Field rename:** `role: "admin"|"builder"` (was `"admin"|"member"`).

### `/api/memberships/invite` & `PATCH /api/memberships/:user_id`

- **Field rename:** request `role` accepts `"admin"|"builder"`.

### `/api/lessons` (rename + extend, see Renames + per-surface table)

- Adds search/filter params per the Tickets-list pattern.
- Response gains `created_by`.

### `/api/events` — SSE event kinds

New event kinds emitted by backend; consumed by SPA:

| Kind | Source | Consumer |
|---|---|---|
| `notification_created` | Notifications module | Popover unread badge + page invalidation |
| `notification_read` | Notifications module | Same |
| `hitl_pending` | Workflow engine on `awaiting_human` | Ticket detail HITL tab |
| `hitl_resolved` | Workflow engine on resume | Same |
| `finding_acked` | Reviewer module | Ticket detail Findings tab |
| `finding_pushed_back` | Reviewer module | Same |

`hitl_pending`/`hitl_resolved` may already exist in some form — **verify**, see Open questions.

### `/api/coding-agents/:plugin_id` (PATCH) — settings shape

The opaque `settings` dict gets a versioned Pydantic schema for the Claude Code plugin specifically:

```
ClaudeCodeSettings = {
  orchestrator: {
    model: str,
    effort: "low"|"medium"|"high"|"max",
    use_default_system_prompt: bool,
    system_prompt: str | null
  },
  sub_agents: [          # ≤ 8
    {
      name: str,
      model: str,
      effort: "low"|"medium"|"high"|"max",
      use_default_system_prompt: bool,
      system_prompt: str | null
    }
  ],
  mcp_proxy_ids: [UUID]  # configured MCP proxy connections referenced for context
}
```

Validation in `apps/backend/app/plugins/claude_code/`. Backwards-compatible read (older opaque settings still parse).

---

## Deletions

No outright deletions in M06. Two endpoints become **unused** but stay live:

- `GET /api/reviewer/jobs/by-ticket/:ticket_id` — superseded by stage indicator on Ticket detail. Kept for completeness; SPA stops calling it.
- `GET /api/reviewer/conversations/by-ticket/:ticket_id` — superseded by inline finding expand (E2a.4). Kept; SPA stops calling it.

One endpoint is **functionally replaced**:

- `GET /api/settings/onboarding` — replaced by `GET /api/orgs/config-status`. Stays during F1 Phase 2 → Phase 5; **deleted in F1 Phase 9 cleanup** once no caller remains.

The `GET /api/settings/plugins` endpoint stays as-is — it's a general plugin registry, used by `/api/plugins/available?type=…` callers indirectly. Not in scope.

---

## Cross-cutting concerns

### Role rename (`member` → `builder`)

Touches:

- `apps/backend/app/domain/orgs/permissions.py` — `MembershipRole` enum.
- `apps/backend/app/domain/orgs/memberships_*` — every response that includes `role`.
- `apps/backend/app/domain/identity/account_web.py` — `_AccountMeResponse.orgs[].role`.
- `apps/backend/app/core/auth/*` — wherever role is read from session/membership.
- Database migration: enum type + `UPDATE org_memberships SET role='builder' WHERE role='member'`.
- SPA: every `role === "member"` check becomes `role === "builder"`.

No backward-compat shim — yaaos is in POC per CLAUDE.md, and this is exactly the kind of breaking-rename the project explicitly avoids cushioning.

### Org-scoping (M01 → M06)

Three routers still use `M01_ORG_ID`:

- `apps/backend/app/domain/tickets/web.py` (3 endpoints).
- `apps/backend/app/domain/memory/web.py` (4 endpoints; concurrent with the rename to `lessons`).
- `apps/backend/app/domain/reviewer/web.py` (8 endpoints).

All three get the M03 treatment: `X-Org-Slug` header resolution + a new action constant per module (`TICKETS_READ`, `LESSONS_READ`, `REVIEWER_READ` / `REVIEWER_WRITE`), gated via `require(Action.*)` dependencies. Permission table extended in `permissions.py`.

### Ticket status vocabulary

Backend `TicketStatus` is `open | in_review | complete | abandoned`. M06 SPA uses `running | hitl | done | failed | cancelled`. **Decision:** project workflow state onto ticket response — backend computes `status: M06Status` from the ticket's active workflow execution at read time. No DB migration to the ticket table itself. The original `ticket.status` field stays as an internal coarse marker; SPA never reads it directly.

The projection rule lives at `apps/backend/app/domain/reviewer/workflow_review_view.py` already (see `awaiting_human → running` mapping in the file) — extend it to produce the full M06 vocabulary and call it from the ticket projection.

**Single-stage projection** (every M06 ticket today — `pr_review_v1` workflow):

| `WorkflowState` | M06 ticket status |
|---|---|
| `pending` | `running` |
| `running` | `running` |
| `awaiting_agent` | `running` |
| `awaiting_human` | `hitl` |
| `done` | `done` |
| `failed` | `failed` |
| `cancelled` | `cancelled` |

The current `workflow_review_view.py` mapping (`awaiting_human → running`) is **wrong for M06** — M06 surfaces HITL as a distinct top-level status because the Dashboard's `hitl_pending` count and the Tickets list's `hitl` filter need it. Update the mapping in F1 Phase 3 (Tickets list anchor).

**Multi-stage projection** (future; not in M06 but shape decided now to avoid re-litigation):

Aggregate over the ticket's stages, in this precedence order — first match wins:

1. Any stage `awaiting_human` → `hitl`
2. Any stage `failed` → `failed` (a downstream stage's failure dominates)
3. Any stage `running` / `awaiting_agent` / `pending` → `running`
4. Any stage `cancelled` (and none above) → `cancelled`
5. All stages `done` → `done`

### Builder identity convention

Every endpoint that returns a "who triggered this" field uses the shape:

```
builder: {
  kind: "user" | "system",
  user_id: UUID | null,           # null when kind = "system"
  display_name: str,              # "yaaos" when kind = "system"
  avatar_url: str | null          # null when kind = "system" (SPA renders the logo)
}
```

Applies to: `TicketRow.builder`, `Notification` (if applicable), audit `actor` projections, lesson `created_by`.

For PR-triggered tickets where the PR author isn't a yaaos user yet, `kind = "system"` with `display_name` derived from the PR author login is **wrong** — instead, leave `builder.user_id = null`, `kind = "user"`, `display_name = author_login`. The "system" kind is reserved for genuinely automated triggers (scheduled scans, ops alerts) per A1's terminology lock.

---

## Open questions

These need resolution before F1 Phase 3 (Tickets-list anchor) starts, but don't block Phase 2 (chrome).

1. **HITL event kinds** — Do `hitl_pending` / `hitl_resolved` events emit today from the workflow engine? If yes, just consume; if no, add them in Phase 2 backend chore (small).
2. **TOTP recovery codes** — Does the current TOTP enrollment emit recovery codes? If not, `GET /api/account/totp/recovery-codes` is a slightly bigger lift (codes need to be generated + hashed at enroll, not retrofitted). If recovery codes aren't critical for M06, defer the endpoint to a future security hardening pass and leave the UI section as a placeholder.
3. **`last_used_at` source** — column on `org_memberships`, or derived from sessions? Lean toward the column for simplicity, updated on every successful org-scoped request.
4. **`findings_count` denormalization** — Trigger-maintained column, or computed on read? POC default = computed on read (one join); revisit if Tickets list ever slows.
5. **Lessons `created_by`** — add `actor_user_id` column to `lessons`, or join through audit log? Lean toward the column.
6. **OAuth callback rename strategy** — redirect handler at old path vs upstream OAuth app re-config. Decide in implementation; redirect handler is safer.
7. **`/api/notifications` SSE channel** — does the channel filter by user (server-side) or by org-membership union (server-side)? Lean toward "filter by `user_id = current_user`" — notifications are user-scoped, not org-scoped.

---

## Cross-references

- Drives F1 phases:
  - **Phase 1** — token + primitive substrate. No API changes.
  - **Phase 2** — chrome + IA rules + route renames. Lands: API renames (memory→lessons, integrations→mcp-proxy, byok→api-keys), role rename, org-scoping of M01-era routers, `/api/orgs/mine`, `/api/orgs/config-status`.
  - **Phase 3** — Tickets list anchor. Lands: `/api/tickets` extensions.
  - **Phase 4** — Coding Agent detail anchor. Lands: `ClaudeCodeSettings` schema extension.
  - **Phase 5** — Dashboard anchor. Lands: `/api/tickets/dashboard`.
  - **Phase 6** — Ticket detail anchor. Lands: stage indicator response extension, finding ack/push-back, HITL respond+history, new SSE event kinds.
  - **Phase 7** — Tier 2 derived. Lands: Notifications module + popover endpoint + SSE kinds; `/api/lessons/:id`.
  - **Phase 8** — Tier 3 derived. Lands: `/api/auth/sso/discover`, `/api/orgs` (create), `/api/account/totp/recovery-codes` (if not deferred).
  - **Phase 9** — Cleanup. Lands: delete `/api/settings/onboarding` and any other dead routes.

- Backend per-module docs that need updating in the same PR as each change: `apps/backend/docs/domain_*.md` for each module touched (tickets, lessons, reviewer, notifications [new], orgs, identity).
