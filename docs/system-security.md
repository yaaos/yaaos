# System security

> What's actually shipped today. Every section is backed by a real code path — no aspirational content.

## Trust boundaries

| Boundary | What crosses | Direction |
|---|---|---|
| **GitHub webhook → backend** | HMAC-signed JSON payload | Inbound to `POST /api/intake/github_pr` ([`plugins/github/intake_type.py`](../apps/backend/app/plugins/github/intake_type.py)). |
| **Backend ↔ WorkspaceAgent** | AgentCommand + AgentEvent JSON over HTTPS long-poll + WebSocket. Bearer in `Authorization`. | Outbound-only from agent's TCP perspective. |
| **WorkspaceAgent ↔ workspace process** | JSON-newline IPC over stdin/stdout. | Bidirectional, in-process, no TCP. |
| **Workspace process ↔ Claude Code subprocess** | CLI argv + env + stdio. | Local subprocess. |

**Critical property:** the workspace process holds no credentials for the yaaos control-plane API. Findings cross the trust boundary only via the supervisor's `POST /api/v1/commands/{id}/events`, which is the audited piece.

## Control-plane security

### Authentication

- **User sessions** — `core/sessions` issues session + CSRF cookies on OAuth callback. The default-deny `core/auth.AuthMiddleware` classifies every `/api/*` path as `PUBLIC`, `USER_SCOPED`, or `ORG_SCOPED`; routes declare matching deps (`public_route` / `require_session` / `require(action)`); a post-response guard 500s any 2xx that left `route_security_resolved` unset.
- **WorkspaceAgent bearer** — issued by `POST /api/v1/agent/identity` after STS replay verification. 1-hour TTL; the agent re-exchanges before expiry. The org-side trust anchor is `orgs.registered_iam_arn`; `core/agent_gateway.ensure_agent_row` finds or creates the `workspace_agents` row keyed on `(org_id, instance_id)`.

### Authorization

- Per-action `Role` mapping in [`core/auth/role_policy._REQUIRED_ROLE`](../apps/backend/app/core/auth/role_policy.py): `BUILDER < ADMIN < OWNER`. `Role` and `Action` enums in [`core/auth`](../apps/backend/docs/core_auth.md).
- Owner/Admin-gated endpoints: `PATCH /api/orgs` (registered_iam_arn + aws_region), `GET /api/workspaces/connection_status`.

### Secrets at rest

- `core/secrets` Fernet-encrypts everything that lives in the database. Key is `YAAOS_ENCRYPTION_KEY` env var — never derived, never hard-coded.
- Encrypted columns: `byok_keys.encrypted_value`, `sso_configs.sp_private_key_encrypted`, `user_totp_secrets.encrypted_secret`.
- Platform GitHub App private key + webhook secret live in env vars (`YAAOS_GITHUB_APP_PRIVATE_KEY`, `YAAOS_GITHUB_APP_WEBHOOK_SECRET`), not the DB.

### Audit log

[`core/audit_log`](../apps/backend/docs/core_audit_log.md) writes one row per state transition. Required-session API (see [`apps/backend/docs/patterns.md`](../apps/backend/docs/patterns.md)) ensures audit rows always commit alongside the state change — no diverging audit-only or state-only writes.

## Agent + workspace security

### IAM trust anchor

Each customer registers an IAM-role ARN at `PATCH /api/orgs` (`registered_iam_arn`). The agent in their ECS task assumes that role; `POST /api/v1/agent/identity` replays the agent's sigv4-signed `GetCallerIdentity` against AWS STS, canonicalizes the assumed-role ARN, and matches it against the registered ARN. The trust chain is AWS STS signature verification — yaaos never trusts the agent's own ARN claim.

**Audience binding** — the `X-Yaaos-Audience` header in the signed payload must be present and match `YAAOS_PUBLIC_HOSTNAME` (the backend's required canonical hostname). Missing or mismatched → 401 `audience_mismatch`. This prevents a valid signature produced for one yaaos instance from being replayed against another. `YAAOS_PUBLIC_HOSTNAME` is a required boot-time setting; the backend refuses to start without it.

**Host allowlist** — the STS endpoint URL in the signed request is validated against a regex allowlist of known AWS STS hostnames. In non-prod, `YAAOS_STS_HOST_OVERRIDE` extends the allowlist to admit a mock STS host; the process refuses to boot if `YAAOS_ENV=prod` and the override are set simultaneously.

### Workspace isolation (what ships)

- **OS-process isolation per workspace** — supervisor spawns one OS process per workspace; IPC over stdin/stdout pipes.
- **Container filesystem read-only** except `/var/agent/workspaces/` (documented in [`apps/agent/docs/README.md`](../apps/agent/docs/README.md)).

### What deliberately doesn't ship

No landlock / seccomp / per-workspace UID / network namespaces. Risk surface: the workspace process (single tenant, customer code already trusted to that level) + the supervisor (which holds the control-plane bearer, audited via structlog + OTel spans).

### Zero biz logic in the agent

Every threshold, prompt, lesson, depth, and timeout is supplied by the control plane via AgentCommand payload. The agent is OS-process scheduling + IPC framing + repo clone + Claude Code subprocess management — no policy. Changing review behavior is a control-plane deploy; the customer's deployed agent doesn't roll forward.

## Wire-protocol security

### TLS

Always. The agent only opens outbound TLS connections to the control plane; no inbound TCP from yaaos. ECS task definitions don't expose any ports.

### Bearer scope

The bearer issued at identity-exchange is scoped to the per-agent-instance `agent_id` (`workspace_agents.id`). It travels in `Authorization: Bearer <token>` on every HTTPS endpoint + the WebSocket upgrade.

### Single-flight + stale-claim guard

[`core/workspace.try_claim`](../apps/backend/app/core/workspace/dispatch.py) is an atomic conditional UPDATE succeeding iff `current_command_id IS NULL AND status='active'`. Concurrent dispatch attempts see `rowcount=0` and back off — only one AgentCommand can hold a workspace at a time.

Event endpoints (`POST /api/v1/commands/{id}/events`, `POST /api/v1/workspaces/{id}/events`) validate inbound `command_id` against `workspaces.current_command_id`. Mismatch → `410 Gone`; agent abandons silently.

### Failure-report-precedes-disposal

`release_claim` clears `current_command_id` but **preserves** `current_holder_workflow_id`. The terminal event must arrive before the workspace row is disposed — workflows can never lose their resolution path to the workspace that owned them.

### `traceparent` on every wire payload

W3C trace context is a required field on every AgentCommand, AgentEvent, WorkspaceEvent, and Heartbeat. The intake endpoint records `current_traceparent()` at webhook arrival; the workflow execution row carries it forward; tasks restore it via [`core/observability.with_remote_parent_span`](../apps/backend/app/core/observability/traceparent.py). One trace_id covers webhook → terminal outcome across providers.

## Data at rest

| Class | Where | Encryption |
|---|---|---|
| OAuth identity + sessions | `users`, `oauth_identities`, `sessions` | Refresh tokens in `oauth_identities.encrypted_refresh_token` (Fernet). Session bearers sha256-hashed — raw value only on user's cookie. |
| BYOK provider keys | `byok_keys.encrypted_value` | Fernet via `core/secrets`. |
| GitHub webhook secret + App private key | `YAAOS_GITHUB_APP_WEBHOOK_SECRET` + `YAAOS_GITHUB_APP_PRIVATE_KEY` env vars | Platform-deployment secrets (env vars, not DB). One App per yaaos deployment; never per-customer. |
| SAML SP private key | `sso_configs.sp_private_key_encrypted` | Fernet via `core/secrets`. |
| TOTP secrets | `user_totp_secrets.encrypted_secret` | Fernet via `core/secrets`. |
| MCP review bearer | `mcp_review_tokens.token_hash` | sha256 — raw value never persists. |
| Activity events | n/a — never persisted | n/a |

## Threat model

| Threat | Defense |
|---|---|
| Inbound webhook from an attacker not GitHub | HMAC verification in [`plugins/github/service.verify_webhook_signature`](../apps/backend/app/plugins/github/service.py); intake type returns 401 on mismatch. |
| Duplicate webhook delivery | `X-Github-Delivery` is the `idempotency_key` on `domain/tickets.create()`; second submission returns the same ticket without starting a new workflow. |
| Stale event redelivery from a workspace whose claim has rotated | Stale-claim guard returns 410; agent abandons. |
| Two workflows racing the same workspace | Single-flight `try_claim` atomic CAS. |
| Agent identity spoofing | STS replay verification: backend replays the agent's sigv4-signed `GetCallerIdentity` against AWS STS; trust anchored to AWS signature verification + audience binding. |
| Activity events leaking source content | Not yet defended — WebSocket plumbing exists but no pre-renderer audit on `domain/coding_agent` ActivityEvents. |
| Worker exhaustion under long-running AgentCommands | Async event-driven engine — workers exit after dispatch and resume on terminal event. Verified by workflow state-machine tests. |

## Not defended against (yet)

- Compromised agent instance (customer's IAM role + workspace state on disk). Out of scope — workspace-process sandbox hardening is deferred.
- Activity event payload tampering. WebSocket is TLS-protected but events are not signed. Architectural assumption: customer's network is trusted to ECS.
- Side-channel via prompt content. Out of scope.

## Cross-references

- [`apps/backend/docs/core_agent_gateway.md`](../apps/backend/docs/core_agent_gateway.md) — wire protocol mechanics.
- [`apps/backend/docs/core_workspace.md`](../apps/backend/docs/core_workspace.md) — single-flight claim + recovery registry + cleanup failsafes.
- [`apps/backend/docs/core_workflow.md`](../apps/backend/docs/core_workflow.md) — engine + state machine.
- [`apps/backend/docs/core_audit_log.md`](../apps/backend/docs/core_audit_log.md) — audit shape + retention.
- [`apps/agent/docs/README.md`](../apps/agent/docs/README.md) — agent deployment + IAM role.
