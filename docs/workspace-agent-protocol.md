# WorkspaceAgent wire protocol

> Control-plane ↔ WorkspaceAgent contract: channels, lifecycle, claim routing, auth, and ordering conventions.

## Channels

Five HTTPS endpoints + one WebSocket under `/api/v1/`. See `apps/backend/openapi/agent-api.yaml` for schemas.

Agent identity on all operational channels is derived solely from the bearer — no `{agent_id}` path segment. The identity exchange endpoint is unauthenticated (it bootstraps the bearer).

| Endpoint | Direction | Purpose |
|---|---|---|
| `POST /api/v1/agent/identity` | Agent → CP | STS-signed bootstrap → 1h bearer |
| `POST /api/v1/agent/heartbeat` | Agent → CP | Liveness + workspace inventory; CP returns reconciliation hints |
| `POST /api/v1/agent/commands/claim` | Agent → CP | Long-poll for next command (≤55s) |
| `POST /api/v1/commands/{id}/events` | Agent → CP | Progress + terminal AgentEvent |
| `POST /api/v1/workspaces/{id}/events` | Agent → CP | Workspace state transitions |
| `WSS /api/v1/agent/activity` | Bidirectional | High-frequency activity streaming; demand-pull |

## `unconfigured → configured` state machine

A fresh agent (or any restarted agent instance) enters the `unconfigured` lifecycle.

**Unconfigured:**
- Claim requests carry `lifecycle="unconfigured"`.
- The control plane returns a `ConfigUpdateCommand` (kind `"ConfigUpdate"`) on every unconfigured claim, regardless of queue depth.
- Workspace commands are not dequeued; they accumulate until the agent is configured.
- The agent rejects any `WorkspaceCommand` that arrives before configuration with `completed_failure "agent unconfigured"`.

**Transition:** `ConfigUpdateCommand.Execute` stores the config atomically. The agent's lifecycle immediately becomes `configured`.

**Configured:**
- Claim requests carry `lifecycle="configured"`, `new_workspaces` (capacity for new workspaces), and `workspace_ids` (idle Active workspaces awaiting a command).
- The backend draws a batch from `agent_commands`: up to `new_workspaces` unassigned `CreateWorkspace` rows + one pending row per named `workspace_id`.
- A process restart returns to `unconfigured` (the atomic pointer is not persisted).

## Claim routing — capacity-pull

The `ClaimRequest` body:
- `new_workspaces = max_workspaces − active count` — capacity for new workspaces.
- `workspace_ids` — idle Active workspaces (Active registry records with no in-flight command).

`claim_next` returns **exactly one** command per call via a single `FOR UPDATE SKIP LOCKED LIMIT 1` across the eligible set (FIFO by UUIDv7 id):
1. A pending unassigned `CreateWorkspace` row (when `new_workspaces > 0`).
2. The oldest pending row pinned to this agent for any `workspace_id` in `workspace_ids`.

Returns 204 when nothing eligible. Each concurrent claim worker gets at most one row per call, leaving no rows stranded in `claimed` limbo from batch over-claim.

## Command lease + `received` event

After the claim succeeds and the command is decoded, the agent posts `kind=received` to `/api/v1/commands/{id}/events`. This flips the backend row from `claimed → delivered`, cancelling the 30-second lease requeue. Without a `received` event the backend requeues the row to `pending` on the next `cleanup_loop` tick (up to `MAX_ATTEMPT=5` times before permanent retirement to `done`).

## Bearer auth + renewal

- The agent sigv4-signs a `GetCallerIdentity` via `identity.awsSTSProvider`; the backend replays it against AWS STS (or mock-aws in dev/test) and issues a 1-hour bearer.
- The response includes `renewal_after` (5 min before `expires_at`); the supervisor re-exchanges at that time (`bearerRefreshLoop`).
- Renewal is non-revoking — the old bearer stays valid to its own `expires_at`. The agent atomically swaps the bearer after rotation.
- A renewal that returns different `agent_id`, `org_id`, or `instance_id` than the first exchange is an identity-integrity violation; the agent exits fatally.
- The agent pins `agent_id`, `org_id`, and `instance_id` from the first exchange and carries them on every log/span/metric.

## Bootstrap-retry asymmetry

**Unbootstrapped agent** (identity exchange never succeeded): `stsBackoff` has a 1-hour max-elapsed deadline. After 1 hour of continuous failure the agent calls `os.Exit(1)` so the container orchestrator restarts it. A misconfigured ARN that won't fix itself in 1h becomes a loud crash rather than a silent retry loop.

**Bootstrapped agent** (at least one successful exchange): bearer renewal failures use the indefinite `heartbeatBackoff`/`claimBackoff` ramp. A transient STS blip must not kill a running agent that holds active workspaces.

## Heartbeat body

`POST /api/v1/agent/heartbeat` body: `reported_at` (ISO-UTC), `workspaces[]` (array of `{workspace_id, status, current_command_id?}`).

The backend derives `workspace_agents.claimed_workspace_count` from `len(workspaces)` on every heartbeat — it is not a wire field the agent supplies explicitly.

## Identity wire format

`POST /api/v1/agent/identity` request body fields: `kind` (`"aws-sts"`), `agent_version`, `agent_metadata` (`os`, `cpu_count`, `memory_bytes`), `payload` (sigv4-signed STS envelope JSON).

Response: `bearer`, `expires_at`, `renewal_after`, `agent_id`, `instance_id` (backend-derived from role-session-name), `org_id`.

The `X-Yaaos-Audience` header inside the signed `payload` must be present and match `YAAOS_PUBLIC_HOSTNAME` (the backend's required canonical hostname; boot fails without it). Missing or mismatched → 401 `audience_mismatch`. See [`apps/backend/docs/core_agent_gateway.md`](../apps/backend/docs/core_agent_gateway.md) for the full identity exchange contract.

## Ordering + idempotency

- Commands are FIFO within the durable `agent_commands` queue, ordered by UUIDv7 PK.
- Each command carries a `command_id` (UUID). The stale-claim guard on the backend matches the posted event's `command_id` against the workspace's current claim; a mismatch returns `410 Gone`.

## At-least-once delivery + dedup

**Re-delivery:** the control plane may re-deliver a `command_id` after a transient ACK failure. The agent never re-executes a re-delivered command.

**Dedup cache:** the agent keeps a bounded in-memory LRU (1024 entries, `command_id → terminal AgentEvent`). On a re-delivered `command_id`, the cached terminal event is replayed through the retry loop — no dispatch to the workspace subprocess.

**Terminal-event retry:** after each dispatch the agent retries `POST /api/v1/commands/{id}/events` with backoff (1s/2s/5s/10s/30s ramp, last step pins). Two stop conditions:
- Success (2xx) — done.
- `410 Gone` (`ErrStaleClaim`) — the backend no longer holds the claim; the event is dropped silently.

**Progress events** are best-effort single-shot; only terminal events use the retry loop.

**Crash loss:** the dedup cache is in-memory only. An agent restart clears it; re-delivered commands after a restart are re-executed (at-least-once guarantee, not exactly-once).

## ISO-UTC wire convention

All `datetime` fields use ISO 8601 with `Z` suffix (UTC). Pydantic emits `Z`-suffixed strings; the Go agent formats with `time.RFC3339`.

## Schema reference

`apps/backend/openapi/agent-api.yaml` — authoritative spec. `app/core/agent_gateway/types.py` is the hand-written Pydantic mirror; drift is detected by `test_openapi_mirror_drift.py`. The Go agent's wire types live in `apps/agent/internal/protocol/types.go`.
