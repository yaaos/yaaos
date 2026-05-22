# System security

> What's actually shipped today. Every section here is backed by a real code path — no aspirational content. The unfinished agenda lives in [`plan/notes/security-posture.md`](../plan/notes/security-posture.md).

## Trust boundaries

Three processes hold different secrets and run with different privileges.

| Boundary | What crosses | Direction |
|---|---|---|
| **GitHub webhook → backend** | HMAC-signed JSON payload | Inbound to `/api/intake/github_pr` ([`apps/backend/app/plugins/github/intake_type.py`](../apps/backend/app/plugins/github/intake_type.py)). |
| **Backend ↔ WorkspaceAgent** | AgentCommand + AgentEvent JSON over HTTPS long-poll + WebSocket activity stream. Bearer in `Authorization`. | Outbound-only from the agent's TCP perspective. |
| **WorkspaceAgent ↔ workspace process** | JSON-newline IPC over stdin/stdout. | Bidirectional, in-process, no TCP. |
| **Workspace process ↔ Claude Code subprocess** | CLI argv + env + stdio. | Local subprocess. |

**Critical property:** the workspace process holds no credentials for the yaaos control-plane API. Findings cross the trust boundary only via the supervisor's `POST /api/v1/commands/{id}/events` call, which is the audited piece.

## Control-plane security

### Authentication

- **User sessions** — `domain/sessions` issues session + CSRF cookies on OAuth callback. M02 default-deny middleware (`core/auth.AuthMiddleware`) gates every route via `Depends(require(action))` or `Depends(public_route)`; missing declaration is caught by a post-response guard.
- **WorkspaceAgent bearer** — placeholder verifier today (any non-empty `Bearer <token>` after a successful identity-exchange). Real SigV4-signed STS replay lands in the Phase 7 follow-on; the org-side trust anchor (`orgs.registered_iam_arn`) is in place and `core/agent_gateway.ensure_agent_row` is ready to consume the verified ARN.

### Authorization

- Per-action `Role` mapping in [`domain/sessions/dependencies._REQUIRED_ROLE`](../apps/backend/app/domain/sessions/dependencies.py): `MEMBER` < `ADMIN` < `OWNER`. `Action` enum in [`core/auth/types.py`](../apps/backend/app/core/auth/types.py).
- Owner/Admin-gated M05 endpoints: `PATCH /api/orgs` (workspace_provider + registered_iam_arn), `GET /api/workspaces/connection_status`.

### Secrets at rest

- `core/secrets` Fernet-encrypts everything that lives in the database. Encryption key is the `YAAOS_ENCRYPTION_KEY` env var; never derived, never hard-coded.
- Encrypted columns: `byok_keys.encrypted_value` (per-(org, provider) BYOK key), `github_settings.encrypted_webhook_secret`, `sso_configs.sp_private_key_encrypted`, `user_totp_secrets.encrypted_secret`.

### Audit log

[`core/audit_log`](../apps/backend/docs/core_audit_log.md) writes one row per state transition. Required-session API ([`apps/backend/docs/patterns.md` § Session management + atomicity](../apps/backend/docs/patterns.md)) means audit rows always commit alongside the state change they describe — no diverging audit-only or state-only writes.

## Agent + workspace security

### IAM trust anchor

Each customer registers an IAM-role ARN at `PATCH /api/orgs` (`registered_iam_arn`). The agent in their ECS task assumes that role; the placeholder identity-exchange verifier today accepts any non-empty signed-STS string, and the Phase 7 follow-on replays the SigV4 signature against AWS STS and verifies the resulting ARN matches the org's registration.

### Workspace isolation (what M05 ships)

- **OS-process isolation per workspace** — the supervisor spawns one OS process per workspace; IPC over stdin/stdout pipes. Phase 6 foundations ships the supervisor skeleton; the workspace subprocess body lands in the follow-on.
- **Container filesystem read-only** except `/var/agent/workspaces/` (deployment configuration; documented in [`apps/agent/docs/README.md`](../apps/agent/docs/README.md)).
- **`os.RLimit` per workspace process** — Phase 6 follow-on, alongside the subprocess body.

### What M05 deliberately doesn't ship

- No landlock / seccomp / per-workspace UID / network namespaces. The risk surface is the workspace process (single tenant, customer code already trusted to that level) + the supervisor (which holds the control-plane bearer, audited via the structured log + OTel spans).

### Zero biz logic in the agent

Every threshold, prompt, lesson, depth, and timeout is supplied by the control plane via AgentCommand payload. The agent is OS-process scheduling + IPC framing + repo clone + Claude Code subprocess management — no policy. Changing review behavior is a control-plane deploy; the customer's deployed agent doesn't roll forward.

## Wire-protocol security

### TLS

Always. The agent only opens outbound TLS connections to the control plane; no inbound TCP from yaaos. ECS task definitions don't expose any ports.

### Bearer scope

The bearer issued at identity-exchange is scoped to the per-pod `agent_id` (`workspace_agents.id`). It travels in `Authorization: Bearer <token>` on every HTTPS endpoint + the WebSocket upgrade. Phase 7 follow-on adds short-lived issuance (proactive refresh) + revocation tied to `workspace_agents.state`.

### Single-flight + stale-claim guard

[`core/workspace.try_claim`](../apps/backend/app/core/workspace/dispatch.py) is an atomic conditional UPDATE that succeeds iff `current_command_id IS NULL` AND `status='active'`. Concurrent dispatch attempts see `rowcount=0` and back off — only one AgentCommand can hold a workspace at a time.

Event endpoints (`POST /api/v1/commands/{id}/events`, `POST /api/v1/workspaces/{id}/events`) validate the inbound `command_id` against `workspaces.current_command_id`. Mismatch → `410 Gone`; the agent abandons silently. This guards against late-redelivered events from a stale command claim.

### Failure-report-precedes-disposal

`release_claim` clears `current_command_id` but **preserves** `current_holder_workflow_id`. The terminal event must arrive before the workspace row is disposed, so workflows can never lose their resolution path to the workspace that owned them.

### `traceparent` on every wire payload

W3C trace context is a required field on every AgentCommand, AgentEvent, WorkspaceEvent, and Heartbeat. The intake endpoint records `current_traceparent()` at webhook arrival; the workflow execution row carries it forward; tasks restore it via [`core/observability.with_remote_parent_span`](../apps/backend/app/core/observability/traceparent.py). One trace_id covers webhook → terminal outcome across providers (verified by unit tests of the helpers; full E2E rides on Phase 4 + 6 follow-on integration).

## Data at rest

| Class | Where | Encryption |
|---|---|---|
| OAuth identity + sessions | `users`, `oauth_identities`, `sessions` | Refresh tokens in `oauth_identities.encrypted_refresh_token` (Fernet). Session bearers hashed (sha256) — raw value only on the user's cookie. |
| BYOK provider keys | `byok_keys.encrypted_value` | Fernet via `core/secrets`. |
| GitHub webhook secret | `github_settings.encrypted_webhook_secret` | Fernet via `core/secrets`. |
| SAML SP private key | `sso_configs.sp_private_key_encrypted` | Fernet via `core/secrets`. |
| TOTP secrets | `user_totp_secrets.encrypted_secret` | Fernet via `core/secrets`. |
| MCP review bearer | `mcp_review_tokens.token_hash` | sha256 — raw value never persists. |
| Activity events | n/a — never persisted | n/a |

## Threat model — what M05 explicitly defends against

| Threat | Defense |
|---|---|
| Inbound webhook from an attacker not GitHub | HMAC verification in [`plugins/github/service.verify_webhook_signature`](../apps/backend/app/plugins/github/service.py); intake type returns 401 on mismatch. |
| Duplicate webhook delivery | `X-Github-Delivery` is the `idempotency_key` on `domain/tickets.create()`; second submission returns the same ticket without starting a new workflow. |
| Stale event redelivery from a workspace whose claim has rotated | Stale-claim guard returns 410; agent abandons. |
| Two workflows racing the same workspace | Single-flight `try_claim` atomic CAS. |
| Agent pod identity spoofing | Phase 7 follow-on — SigV4-signed STS replay against AWS. The placeholder verifier today does NOT defend against this; foundations only. |
| Activity events leaking source content | `domain/coding_agent` ActivityEvent pre-renderer audit — Phase 8b follow-on. Foundations: the WebSocket plumbing exists; the trust-boundary audit lands alongside the in-memory provider's direct-publish path. |
| Worker exhaustion under long-running AgentCommands | Async event-driven workflow engine — workers exit after dispatch and resume on the terminal event. Verified by the workflow state-machine tests. |

## Threats M05 does NOT defend against (yet)

- Compromised agent pod (running as customer's IAM role with workspace state on disk). Out of scope per architecture — workspace-process sandbox hardening is post-M05.
- Activity event payload tampering. WebSocket is TLS-protected but events are not signed. Architectural assumption: customer's network is trusted to ECS.
- Side-channel via prompt content. Out of scope.

## Cross-references

- [`apps/backend/docs/core_agent_gateway.md`](../apps/backend/docs/core_agent_gateway.md) — wire protocol mechanics.
- [`apps/backend/docs/core_workspace.md`](../apps/backend/docs/core_workspace.md) — single-flight claim + recovery registry + cleanup failsafes.
- [`apps/backend/docs/core_workflow.md`](../apps/backend/docs/core_workflow.md) — engine + state machine.
- [`apps/backend/docs/core_audit_log.md`](../apps/backend/docs/core_audit_log.md) — audit shape + retention.
- [`apps/agent/docs/README.md`](../apps/agent/docs/README.md) — agent deployment + IAM role.
- [`plan/notes/security-posture.md`](../plan/notes/security-posture.md) — unfinished security agenda (what's NOT here yet).
