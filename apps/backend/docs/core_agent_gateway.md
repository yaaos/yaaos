# core/agent_gateway

> Wire protocol between the customer-deployed WorkspaceAgent and the yaaos control plane.

## Scope

- **Owns:** inbound WorkspaceAgent surface — identity exchange, long-poll command claim, heartbeat/inventory, AgentEvent + WorkspaceEvent ingestion. `workspace_agents` and `bearer_tokens` tables. `WorkspaceAgentReportSink` Protocol + single-slot registry. `AgentRunSink` Protocol + single-slot registry (run lifecycle).
- **Does not own:** workspace lifecycle (owned by [`core/workspace`](core_workspace.md)); workflow routing (owned by [`core/workflow`](core_workflow.md)); `coding_agent_runs` table (owned by [`core/coding_agent`](core_coding_agent.md)). Never imports `core/workspace` directly — workspace-state access goes through the registered sink.
- **Emits:** terminal AgentEvents → `workflow.handle_agent_event` enqueued via outbox (owned by [`core/tasks`](core_tasks.md)); progress events → `publish_workspace_activity` in [`core/sse`](core_sse.md).

## Endpoint scheme

All agent operational channels live under `/api/v1/agent/...` with identity derived solely from the bearer — no `{agent_id}` path segment:

- `POST /api/v1/agent/identity` — issue bearer (see Identity exchange below)
- `DELETE /api/v1/agent/identity` — graceful shutdown "going away" signal (see below)
- `POST /api/v1/agent/heartbeat`
- `POST /api/v1/agent/commands/claim`
- `POST /api/v1/commands/{id}/events` (per-resource ID retained)
- `POST /api/v1/workspaces/{id}/events` (per-resource ID retained)
- `WSS /api/v1/agent/activity`

The `SubscriberRegistry` keys the WS sender on the bearer-derived `agent_id`.

## Graceful shutdown — `DELETE /api/v1/agent/identity`

The agent sends this as its last act on clean shutdown (SIGTERM/SIGINT), after stopping heartbeat + claim loops and draining the WS. The control plane eagerly:

1. Sets `workspace_agents.state=offline` + `last_shutdown_at=now`.
2. Revokes the bearer (reason `graceful_shutdown`).
3. Calls `WorkspaceAgentReportSink.handle_agent_loss` — expires held workspaces, synthesizes `completed_failure` events for any in-flight `current_command_id` so WorkflowExecutions resume rather than hanging in `AWAITING_AGENT`.
4. Publishes `agent_liveness_changed` SSE so the dashboard flips the card offline without waiting for the sweeper's next tick.

Returns 204. Idempotent — a revoked bearer 401s before the handler runs. Best-effort on the agent side: errors are logged but never prevent process exit.

## Liveness sweeper — `compute_agent_liveness_transitions`

Called on each `_reaper_sweep_once` tick from `core/workspace` (the loop host). Computes and writes `workspace_agents.state` transitions for all rows with a known `last_heartbeat_at`. State machine based on seconds since last heartbeat:

- `< 60 s` → `reachable` (online)
- `60 s – 5 min` → `stale`
- `> 5 min` → `offline`

Writes `state` only on transition (idempotent on the same tick). Returns a list of agent UUIDs that newly became `offline` this sweep. Emits one `agent_liveness_changed` SSE event per transitioned agent via `publish_general_after_commit` on the org's general channel — cache-invalidate only, no state in payload.

Cron-beat dedup (via `scheduled_runs ON CONFLICT DO NOTHING`) is the primary cross-pod exclusivity for `requeue_stale_claimed` and `compute_agent_liveness_transitions`; row-level `FOR UPDATE SKIP LOCKED` on the SELECT in each function is the defense-in-depth backstop against overlapping reaper bodies when a slow sweep crosses a cron-beat boundary.

## `GET /api/orgs/{slug}/agents`

Returns agents for the current org within the 1-hour UI-retention window. Fields: `id`, `instance_id`, `state`, `last_heartbeat_at`, `os`, `cpu_count`, `memory_bytes`, `claimed_workspace_count`, `version`. Excludes agents whose last heartbeat is older than 1 hour (rows stay in the DB). Requires `ORG_READ` (visible to all org members). Implemented in `app/domain/orgs/org_settings_web.py`; delegates to `list_agents_for_org`.

## Identity exchange — `POST /api/v1/agent/identity`

Vault AWS-auth pattern. The agent submits a sigv4-signed STS `GetCallerIdentity` as `payload`; the backend replays it, derives `instance_id` from the role-session-name, and issues a 1-hour bearer.

- **Audience binding** — `X-Yaaos-Audience` in the signed envelope must be present and match the backend's public hostname — the host[:port] (`netloc`) of the required `YAAOS_PUBLIC_ORIGIN` setting, exposed as `settings.yaaos_public_hostname`. Missing or mismatched → 401 `audience_mismatch`. Binds the signed request to the specific backend deployment. Must match `hostFromURL(YAAOS_BACKEND_URL)` (`url.Host`) on the agent side, port included.
- **`instance_id` derivation** — extracted from the role-session-name of the assumed-role ARN (`arn:aws:sts::ACCT:assumed-role/ROLE/SESSION` → `SESSION`). The agent never supplies `instance_id`.
- **Find-or-create keyed on `(org_id, instance_id)`.** The same ECS task restarting keeps the same row; each exchange updates `iam_arn`, `version`, and static OS metadata. `ensure_agent_row` is idempotent under concurrent identity exchanges for the same `(org_id, instance_id)` — a single `INSERT … ON CONFLICT DO UPDATE … RETURNING` closes the race.
- **ConfigUpdate row enqueued atomically.** `enqueue_config_update_for_agent` is called alongside `ensure_agent_row` in the same transaction — a `ConfigUpdate` row lands in `agent_commands` pre-stamped with the `agent_id` so `claim_next` can pick it up without a workspace sweep. Identity-exchange retries enqueue unconditionally; duplicate rows are harmless (`ApplyConfig` is idempotent on the agent, last-write-wins). The row receives a real `completion_token_hash`; the agent echoes the token on its terminal event.
- **1-hour TTL.** Response includes `renewal_after` (5 min before `expires_at`) as the suggested re-exchange time.
- **Non-revoking rotation.** A second call issues a new bearer without revoking the old one. The agent atomically swaps the bearer after receiving the rotation response.
- **`issued_iam_arn` on bearer row.** Every `bearer_tokens` row records the canonical IAM ARN verified at issuance.
- **Host allowlist override** — the `Settings.yaaos_sts_host_override` field (`YAAOS_STS_HOST_OVERRIDE`) allows an additional STS host (e.g. `mock-aws:4566`) only in non-production. `Settings` validates this at config load — a `model_validator` refuses to construct (crashes boot) when `APP_MODE=production` and the override is set — so by the time `sts_verifier` reads it, a non-empty value guarantees non-prod. See [core_config.md](core_config.md).
- **STS replay protection in Redis.** `sts_verifier` stores `sha256(Authorization | X-Amz-Date)` as a Redis key (`sts_nonce:{digest}`) with a 10-minute TTL via `core/redis.set_if_absent`. The first call with a given envelope inserts the key; any subsequent call (from any pod) finds it and raises `REPLAY_DETECTED`. Hash-before-store matches the bearer-token-discipline pattern — Redis never sees the raw Authorization header.

## Dispatch spans

Both `enqueue_command` and `enqueue_command_payload` open an `agent_command.dispatch.{kind}` OTel span covering the full DB insert. Attributes set on the span:

- `kind` — the `AgentCommandKind` string (e.g. `ProvisionWorkspace`, `InvokeClaudeCode`).
- `command_id` — the UUID of the command being enqueued.
- `workspace_id` — the workspace UUID, or `""` for org-scoped commands (`ConfigUpdate`, etc.).
- `workflow_id` — the `workflow_execution_id` UUID, or `""` when the command has no workflow correlation.

`org_id`, `actor_kind`, and other process-wide dimensions are auto-stamped by the `YaaosDimensionsSpanProcessor` — callers must not set them manually. On exception the span records the exception event and sets `StatusCode.ERROR` before re-raising. This is the single span site for all agent-command dispatches; no caller may bypass it.

The span is a child of whatever span is current at the call site (typically a `workflow.command.{kind}` span). On the agent side, `supervisor.dispatch.{kind}` continues the trace via the `traceparent` injected into the `AgentCommand` wire payload.

**`traceparent` ownership rule:** both dispatch functions unconditionally overwrite `traceparent` in the persisted payload with the dispatch span's own traceparent (via `current_traceparent()`). Callers pass `traceparent=""` (or the current context traceparent) as a hint; the dispatch function owns the final value.

**Two dispatch entry points:**
- `enqueue_command(org_id, command, *, session, workflow_execution_id)` — accepts any typed `AgentCommand` instance (the five workspace-lifecycle kinds). Unpacks the command into primitives and delegates to `enqueue_command_payload`.
- `enqueue_command_payload(org_id, *, command_id, kind, workspace_id, payload, traceparent, session, workflow_execution_id)` — accepts primitives only; no vendor-specific type imports required. Used by `core/coding_agent.dispatch_invocation` to enqueue `InvokeClaudeCode` without importing `InvokeClaudeCodeCommand`.

**`InvokeClaudeCodeCommand` / `InvokeClaudeCodeLimits` are package-private.** Both classes remain defined in `core/agent_gateway/types.py` and are members of the `AgentCommand` discriminated union used by `_COMMAND_ADAPTER` for `claim_next` deserialization. They are NOT re-exported from `core/agent_gateway.__init__` (not in `__all__`). Callers outside agent_gateway must not import them — use `enqueue_command_payload` with primitives instead.

Service tests: `test/test_dispatch_service.py` (typed `enqueue_command`); `test/test_enqueue_command_payload_service.py` (primitive `enqueue_command_payload`).

## Command dispatch — `agent_commands` durable queue

Commands are persisted in `agent_commands` before delivery. The queue provides:
- **Durability** — a backend restart with unclaimed commands loses nothing; the rows remain `pending`.
- **Lease** — a 30-second window after `claimed`: the agent must POST `received` to flip `claimed → delivered`. Rows still `claimed` after the window are requeued to `pending` (or retired to `done` at `MAX_ATTEMPT=5`) by `requeue_stale_claimed`, called each `cleanup_loop` tick from `core/workspace`.
- **Capacity-pull** — the claim request carries `new_workspaces` (cap for new `ProvisionWorkspace` rows) and `workspace_ids` (idle workspaces awaiting a command). `claim_next` runs `FOR UPDATE SKIP LOCKED LIMIT 1` across the eligible set, returning exactly ONE command per call. No Redis; no in-process queues; no batch-overshoot into `claimed` limbo.
- **Idempotency** — `command_id` is the PK (UUIDv7 FIFO key); the single-flight claim + stale-claim outcome absorb re-delivery.

`enqueue_command(org_id, command, *, session)` inserts a `pending` row in the caller's transaction (atomic with `try_claim`). `agent_id` is NULL at enqueue; `claim_next` stamps it at claim time. Post-create commands (cleanup etc.) are pre-stamped via `pin_command_to_agent` so `claim_next`'s `workspace_ids` sweep finds them. `enqueue_command_payload` is the primitive-field variant used by callers that build payloads without importing typed AgentCommand subclasses.

`enqueue_config_update_for_agent(agent_id, *, org_id, session)` is the identity-exchange-specific helper: wraps `enqueue_command` for a `ConfigUpdateCommand` built from `get_settings()`, then immediately pre-stamps `agent_id` on the row so the unconfigured-lifecycle claim SELECT can find it by `(kind='ConfigUpdate', agent_id=this agent)` without a workspace sweep. Called in the same transaction as `ensure_agent_row`. Enqueues unconditionally — duplicate rows drain in FIFO order and `ApplyConfig` is idempotent.

The `claim_next` lifecycle gate: `unconfigured` → SELECT for `ConfigUpdate` rows pre-stamped to this `agent_id` (FIFO, `FOR UPDATE SKIP LOCKED`); `configured` → unchanged two-SELECT logic (`ProvisionWorkspace` priority + agent-pinned workspace commands).

The `received` EventKind is non-terminal: it cancels the lease requeue on the row (`claimed → delivered`). Terminal events retire the row to `done`.

## Why / invariants

- **`DELETE /api/v1/agent/identity` runs inside `org_context`** — the same auth chain as all operational endpoints; the bearer-derived identity provides `org_id` + `agent_id`. Not on the public allowlist.
- **`revoke_all_for_arn(arn, reason, session)` revokes by `issued_iam_arn`** — called by `patch_org_settings` on ARN change or clear so old-ARN agents 401 on their next request. Returns the count of revoked rows; caller commits.
- **Region-mismatch failures write an org-level audit row** — kind `identity_exchange_failed`, only when the canonical ARN matched a registered org (so `org_id` is known). Failures that can't be attributed to an org (unregistered ARN, parse/endpoint/replay/AWS rejections) remain structlog-only. The audit payload carries `category`, `attempted_arn`, `source_ip`.
- **Terminal AgentEvent enqueue is in the same transaction as the workspace mirror update** — prevents a workflow from missing its terminal event on crash between the two writes.
- **Stale-claim guard** — events whose `command_id` has no matching `agent_commands` row raise `StaleClaimError`. Both `/commands/{id}/events` and `/workspaces/{id}/events` map it to 410 `{"error": "stale_claim", "detail": …}`. The row may have been retired by an earlier terminal event.
- **Completion capability token gates every AgentEvent kind, including `received`** — authorization binds to the *command*, not the worker's identity. `claim_next` mints a one-time token (`secrets.token_urlsafe(32)`), stores only its sha256 as `agent_commands.completion_token_hash` (raw never persisted), and injects the raw value into the claimed command DTO (`completion_token`). The agent echoes it on the AgentEvent; `record_agent_event` re-hashes the presented `completion_token` and compares it constant-time (`hmac.compare_digest`) against the stored hash. A mismatch raises `StaleClaimError`, mapped to 410. The check runs immediately after the command-row fetch — before any claim release, run-sink call, lean-row materialisation, workflow enqueue, or lease-bump (`received`). This is bearer-token discipline (see [patterns.md § Bearer token discipline](patterns.md)) applied to `agent_commands`: it is churn-proof — an agent whose `(org_id, agent_id)` legitimately rotates on re-auth still completes its in-flight command. Verification is skipped when `completion_token_hash` is NULL (the command never went through `claim_next`, e.g. test-seeded rows). The token is never logged. `received` events pass through the same gate; a mismatch prevents the lease from being bumped (`claimed` status is preserved). `ConfigUpdateCommand` carries `completion_token` so the agent can echo it on its terminal event.
- **Gateway delegates lean-row materialisation to the sink** — on a `ProvisionWorkspace` terminal `completed_success`, `record_agent_event` calls `WorkspaceAgentReportSink.materialise_provision_success(command_id, agent_id)`. The gateway no longer synthesizes a `WorkspaceEvent` or chooses a workspace-event `kind`; the sink owns all workspace-state shaping (provider id, TTL, spec) and the idempotent insert. The Go agent never sends workspace events, so this is the only materialisation path.
- **Workflow correlation via `agent_commands.workflow_execution_id`** — `enqueue_command` stamps the column at enqueue time when a Workspace `WorkflowCommand.dispatch` originates the row (NULL for agent-scoped commands like `ConfigUpdate`). `record_agent_event` reads the column directly off the command row — no workspace-row lookup is involved. The workflow can therefore resume even after the workspace has been torn down (the `failure-report-precedes-disposal` invariant).
- **`WorkspaceAgentReportSink` IoC seam** — `core/workspace` implements the Protocol and registers at its own import time (`workspace/__init__.py`). agent_gateway's service functions call the registered sink for all workspace-state reads/writes; the `agent_gateway → workspace` import edge does not exist. Canonical direction: workspace → agent_gateway. Both single-slot registries (`register_report_sink`, `register_org_arn_lookup`) are idempotent for the same value but raise on a conflicting re-registration, so a double-wiring bug surfaces at boot rather than silently swapping the singleton. Tests that need to swap stubs reach `clear_report_sink` directly from `app.core.agent_gateway.report_sink` (intra-module submodule import).
- **`AgentRunSink` IoC seam** — `core/coding_agent` implements the Protocol and registers `CodingAgentRunSinkImpl` at import time (`coding_agent/__init__.py`). `record_agent_event` calls the registered sink on every terminal AgentEvent; the sink no-ops for non-`InvokeClaudeCode` kinds. `get_run_sink()` returns `None` when the core module hasn't been loaded — graceful degradation in minimal test configs. The sink's `handle_terminal_event` returns `Mapping[str, Any] | None`; a non-`None` return is merged into the local `outputs` dict (sink keys override same-key native values) before `HANDLE_AGENT_EVENT` is enqueued. `CodingAgentRunSinkImpl` returns `None`. See `app/core/agent_gateway/run_sink.py`.
- **`OrgArnLookup` IoC seam** — `/api/v1/agent/identity` needs to resolve a canonical IAM ARN to an org id + aws_region, but `core` cannot import `domain`. `org_arn_lookup.py` declares `OrgArnRef` (a frozen dataclass) + `register_org_arn_lookup` / `lookup_org_by_arn`. `domain/orgs` registers its implementation at import time; the endpoint calls `lookup_org_by_arn` without any `core → domain` edge.
- **`org_context` wrap on every actor-resolving endpoint** — heartbeat, claim, workspace-events, command-events, and the activity WebSocket (entire connection lifetime). Excluded: `/api/v1/agent/identity` (bootstraps the bearer; no agent identity yet).
- **Bearer-derived identity on all operational channels** — `heartbeat`, `claim`, and the activity WebSocket carry no `{agent_id}` path segment; identity is derived entirely from the bearer. The `org_context` wrap blocks cross-org access. Ownership enforcement for per-resource channels (workspace/command events) is unchanged and described below.
- **Per-agent ownership check on workspace/command-event posts** — `post_workspace_event` / `post_command_event` bind `workspace_id` / `command_id`, which resolve to a workspace carrying an owning `agent_id` ([`core/workspace`](core_workspace.md) `WorkspaceRow.agent_id`, set at create-dispatch). The sink resolves the owner (`owning_agent_for_workspace` / `owning_agent_for_command`); when it isn't the bearer's agent, `_require_workspace_owner` raises 403 `forbidden`. A command that resolves to no workspace (e.g. a `ConfigUpdate`, which has no `workspace_id`) or a workspace with a NULL `agent_id` (in-memory/legacy) carries no ownership edge — authorization falls back to the completion-token check in `record_agent_event` (above). A stale `command_id` (no row) returns 410, not 403.
- **`org_id` on the identity-exchange response** — the response carries `org_id` (the `workspace_agents.org_id` for the matched row). The agent pins both `org_id` and `agent_id` on first exchange and verifies they are unchanged on every bearer renewal; a mismatch triggers a fatal exit on the agent side.
- **ARN canonicalization** — `assumed-role/ROLE/SESSION` → `iam::ACCT:role/ROLE`, lowercased. IAM role names are case-insensitive in AWS; lowering both sides avoids mismatches without losing uniqueness.
- **`SubscriberRegistry` is ContextVar-bound and Redis-backed.** `bind_subscriber_registry` is the production DI seam; `subscriber_registry_isolation` autouse fixture resets per test. On WebSocket reconnect it replays `subscribe` for every active route so the agent's rebuilt SubscriptionSet picks up where the old connection left off. `register_sender` establishes the Redis SUBSCRIBE handshake BEFORE snapshotting `agent_routes` for replay — this ordering guarantees that a concurrent `track()` from another pod whose publish lands between sender-registration and snapshot is delivered via pub/sub rather than lost. Duplicate subscribes arriving via both replay and pub/sub are harmless because the agent handles `subscribe` idempotently.
- **Cross-pod subscribe routing uses Redis pub/sub.** `_senders` stays process-local (the WS send callable only exists on the pod where the WS terminates). `track(wfx_id, workspace_id, agent_id)` writes to three Redis keys (`workflow_subscribers:{wfx_id}` ZSET, `workflow_route:{wfx_id}` HASH, `agent_routes:{agent_id}` ZSET scored by `time.time()`) then publishes a typed `AgentWsControlMessage` on `agent_ws_control:{agent_id}`, and returns a `conn_id` string. Callers MUST pass the same `conn_id` to `untrack(workflow_execution_id, conn_id)` when the subscription ends; this removes exactly that subscriber's ZSET member. `_connections` is `dict[UUID, set[str]]` — the same pod may hold multiple concurrent SSE subscribers to the same wfx_id, each with a unique `conn_id`, and each counted independently. `untrack` publishes `unsubscribe` only when ZCARD drops to zero after removing the specific member.
- **`agent_routes:{agent_id}` is a timestamp-scored ZSET (not a plain SET).** Both `track()` and `heartbeat()` ZADD with `score=time.time()`; the sweeper (see below) evicts stale entries by score. This prevents `agent_routes` members from leaking forever when a pod crashes between `track` and `untrack`.
- **`SubscriberRegistry.heartbeat(workflow_execution_id, conn_id, agent_id)`** re-stamps the `workflow_subscribers:{wfx_id}` and `agent_routes:{agent_id}` ZSET scores for this connection so the sweeper doesn't falsely evict a healthy long-lived subscriber. SSE-handler wiring (calling `heartbeat()` on a `_SSE_HEARTBEAT_INTERVAL_SECONDS` cadence) is a follow-up; without it healthy subscribers are evicted by the sweeper at the 60 s mark.
- **`SubscriberReconciler` is the at-most-once safety net.** Runs every 5 s on each web pod. For each agent with a registered sender, reads `agent_routes:{agent_id}` (now a ZSET; members are the wfx_id strings) and per wfx_id checks ZCARD truth: ZCARD ≥ 1 and agent not streaming → re-publish subscribe; ZCARD = 0 and agent is streaming → publish unsubscribe. Launched via `RouteSpec.on_startup` and stopped via `shutdown()` on SIGTERM.
- **`subscriber_sweeper` GCs stale ZSET entries across both key patterns.** A `@scheduled("subscriber_sweeper", "* * * * *")` task runs `ZREMRANGEBYSCORE` on every `workflow_subscribers:*` key AND every `agent_routes:*` key every minute, removing members whose score (unix timestamp) is older than 60 s. Covers crashed SSE pods that never called `untrack`.
- **No activity flows from agent → SPA when nobody's watching** — the `SubscriberRegistry` only sends `subscribe` on `0 → 1` subscriber-count transitions.
- **`seed_agent` lives in `app/testing/seed`.** The production `ensure_agent_row` API is what callers use; `seed_agent` is a test convenience wrapper that adds a random instance_id and optional heartbeat back-dating. Cross-module tests import it from `app.testing.seed`.

## Gotchas

- **Replay-LRU window is 10 min** — clock skew > 5 min on the agent side will produce `clock_skew` rejections.
- Bearer plaintext is returned exactly once from `bearers.issue` and never persisted; `verify` returns `None` for every failure (no oracle).

## Vocabulary

- **AgentConfig.otlp_token** — `SecretStr | None` end-to-end in Python. `.get_secret_value()` is called only at the JSON wire-encode boundary via a `field_serializer(when_used="json")` on the field — `str()`, `repr()`, and `model_dump()` (Python mode) all show `**********`. The wire JSON carries the raw token so the agent can pass it to its OTLP exporter.
- **AgentCommand** — discriminated union: `ProvisionWorkspace | WriteFiles | RefreshWorkspaceAuth | InvokeClaudeCode | CleanupWorkspace | ConfigUpdate`.
- **AgentEvent** — `progress` or `received` (non-terminal) or `completed_{success|failure|skipped}` (terminal). `received` cancels the lease requeue.
- **WorkspaceEvent** — `created | ready | exited | destroyed | failed`.
- **BearerContext** — resolved identity from a verified bearer: `bearer_id`, `agent_id`, `org_id`.

## Data owned

- `workspace_agents` — per-agent-instance identity rows; one per `(org_id, instance_id)`. Columns: `instance_id` (role-session-name from STS ARN), `iam_arn`, `version`, `os`, `cpu_count`, `memory_bytes`, `claimed_workspace_count` (populated by `record_heartbeat` as `len(workspaces)`; not set by identity exchange), `last_heartbeat_at`, `last_shutdown_at`, `state`.
- `bearer_tokens` — `(token_hash, issued_at, expires_at, revoked_at, revoked_reason, last_seen_at, source_ip, issued_iam_arn)`. Revocation reasons: `arn_change` (ARN rotation via settings), `mode_switch`, `disconnect`, `manual_rotate`, `agent_loss` (per-agent), `graceful_shutdown` (DELETE handler). `revoke_all_for_arn` revokes by `issued_iam_arn`; `revoke_all_for_agent` by `agent_id`; `revoke_all_for_org` by `org_id`.
- `agent_commands` — durable command queue. Columns: `id` (UUIDv7 PK = FIFO key), `org_id`, `workspace_id` (NULL for org-scoped commands), `workflow_execution_id` (NULL for agent-scoped commands like `ConfigUpdate`; set by `enqueue_command` when a Workspace `WorkflowCommand.dispatch` originates the row — owns the command→workflow correlation read by `record_agent_event`), `command_kind`, `payload` (JSONB), `status` (`pending|claimed|delivered|done`), `agent_id` (NULL until claimed), `claimed_at`, `attempt`, `created_at`. Indexes: `(agent_id, status, id)` + `(status, command_kind, id)`. CHECK `ck_agent_commands_id_uuidv7` (`uuid_extract_version(id)=7`) enforces the time-ordered FIFO key at the row boundary — producers mint `command_id` app-side with `uuid7()`, and this constraint catches a stray `uuid4` that the semgrep taint rule cannot see across the producer-DTO → `enqueue_command` hop (added `NOT VALID`, so rows predating the guard are grandfathered). See [patterns.md § UUID primary keys](patterns.md).

## How it's tested

`test/test_dispatch_service.py` covers: `enqueue_command` emits an `agent_command.dispatch.{kind}` span with `kind`, `command_id`, `workspace_id`, and `workflow_id` attributes; no-workflow-id enqueue sets `workflow_id` to `""`; a duplicate-PK flush error sets `StatusCode.ERROR` and records an exception event.

`test/test_agent_command_dispatch_traceparent.py` covers: the `traceparent` stored in `agent_commands.payload` carries the dispatch span's own span-id, not the outer caller's — verifying the agent's `supervisor.dispatch.<kind>` will parent to `agent_command.dispatch.<kind>` at runtime.

`test/test_service.py` covers: heartbeat reports unknown workspaces; terminal event enqueues `workflow.handle_agent_event`; progress events publish to the workspace-activity channel but do NOT enqueue; stale `command_id` raises `StaleClaimError`; `has_any_reachable_agent` respects the 90s cutoff.

`test/test_run_sink_return_merges_service.py` covers: a stub `AgentRunSink` returning `{"foo": "bar"}` causes the `HANDLE_AGENT_EVENT` task args to carry `outputs["foo"] == "bar"`; a sink key overrides a same-key native value in `event.outputs`; a sink returning `None` leaves `outputs` equal to `event.outputs`.

`test/test_durable_command_service.py` covers: `enqueue_command` inserts a `pending` row; command survives a simulated backend restart; `claim_next` returns exactly ONE row per call leaving no others in `claimed` limbo; never returns a command for an unlisted workspace; unconfigured claim returns only `ConfigUpdate`; lease: `received` flips `claimed → delivered`; no `received` within 30s requeues to `pending`; terminal event → `done`; attempt cap → `done` (terminal failure); redelivery of `received` is idempotent.

`test/test_claim_lifecycle_service.py` covers `claim_next` lifecycle gate: unconfigured leaves DB rows untouched; configured returns a single ProvisionWorkspace command; empty queue returns `None`.

`test/test_liveness_sweeper_service.py` covers: `compute_agent_liveness_transitions` flips `reachable → stale` at 60s, `stale → offline` and `reachable → offline` beyond 5 min, writes only on transition, returns newly-offline IDs, emits SSE; `GET /api/orgs/{slug}/agents` returns within-retention agents with `claimed_workspace_count`; excludes agents beyond 1h window; excludes other-org agents; requires auth.

`test/test_requeue_stale_claimed_concurrent_service.py` covers: two concurrent `requeue_stale_claimed` calls on independent sessions against the same N stale-claimed rows produce a combined requeue count of exactly N (not 2N) and each row's `attempt` increments by exactly 1 — proving `FOR UPDATE SKIP LOCKED` prevents double-processing.

`test/test_compute_liveness_transitions_concurrent_service.py` covers: two concurrent `compute_agent_liveness_transitions` calls on independent sessions against the same N stale agents produce non-overlapping `newly_offline` lists whose union equals exactly the N seeded agent IDs — proving `FOR UPDATE SKIP LOCKED` prevents the same agent from being transitioned twice.

`test/test_received_token_gate_service.py` covers: `record_agent_event` with `kind=RECEIVED` and a wrong completion token raises `StaleClaimError` and leaves the command row in `claimed` status (lease not bumped); the same with a correct token succeeds and flips the row to `delivered`.

`test/test_identity_exchange.py` covers: happy-path bearer issuance (row persisted by `instance_id`, OS metadata stored, bearer returned with `instance_id` in response); bearer TTL is 1 hour; non-revoking rotation (second call issues new bearer, old stays valid); ARN mismatch → 403; region mismatch → 401; invalid signature → 401; empty payload → 401; unsupported kind → 401; audience mismatch → 401; response includes `org_id` and `instance_id`.

`test/test_queue_binding.py` covers ContextVar isolation for `SubscriberRegistry`: fresh bind hides prior state; fail-fast `RuntimeError` fires before bind.

`test/test_report_sink_delegation.py` covers sink delegation: heartbeat reconciliation via stub sink; workspace-event dispatch and rejection via stub sink; stale-claim guard raises `StaleClaimError` on `accepted=False` outcome.

`test/test_endpoint_authz_service.py` covers per-endpoint authz: `heartbeat` / `claim` reject a missing or empty bearer (401); identity is bearer-derived; `post_workspace_event` / `post_command_event` reject a foreign owning `agent_id` (403) and allow the owner (200); an agent-scoped command (no owning workspace, e.g. ConfigUpdate) is NOT 403'd — it falls through to the stale-claim path (410).

`test/test_command_event_outcome_service.py` covers: stale command returns `410 {"error": "stale_claim"}`; recorded event returns `200 {"command_event_outcome": "event_recorded"}`; active span carries `command_event.outcome="event_recorded"` attribute.

`test/test_config_update_row_service.py` covers: identity exchange enqueues one ConfigUpdate row with `status='pending'` and non-null `completion_token_hash`; unconfigured claim returns the ConfigUpdate row (not a ProvisionWorkspace); configured claim returns ProvisionWorkspace when both kinds are pending; duplicate enqueues produce two claimable rows, both ackable; stale command_id returns 410; `_build_config_update` is not importable.

`test/test_heartbeat_count_service.py` covers `claimed_workspace_count` persistence: heartbeat with N workspaces sets count = N; zero workspaces sets 0; subsequent heartbeat reflects the latest count, not cumulative.

`test/test_activity_publish_service.py` covers the WS `activity_batch` path delivering events to `subscribe_workspace_activity`.

`test/test_graceful_shutdown_service.py` covers: DELETE revokes bearer + sets offline + stamps `last_shutdown_at`; DELETE expires held workspaces + enqueues `handle_agent_event` failure; missing bearer → 401; ARN change/clear via PATCH revokes old-ARN bearers; region-mismatch writes one `identity_exchange_failed` audit row attributed to the org; no-org ARN writes no audit row.

`test/test_subscriber_cross_pod_service.py` covers: two `SubscriberRegistry` instances (two-pod simulation) sharing real Redis; `track` on pod_b delivers `{type:subscribe}` to the sender on pod_a via the `agent_ws_control:{agent_id}` pub/sub channel; `untrack(conn_id=…)` on pod_b delivers `{type:unsubscribe}`; cross-agent isolation (pod_a's sender receives nothing for pod_b's track).

`test/test_subscribers.py` also covers `test_two_subscribers_same_pod_same_wfx`: two concurrent `track()` calls from the same pod to the same wfx_id produce two distinct `conn_id` values and two ZSET members; `untrack(conn_id=conn1)` drops ZCARD to 1 without publishing unsubscribe; `untrack(conn_id=conn2)` drops ZCARD to 0 and publishes unsubscribe. Regression for the pre-fix `dict[UUID, str]` clobber.

`test/test_subscriber_reconciler_service.py` covers: `_reconcile_once()` publishes subscribe when ZCARD ≥ 1 and agent is not streaming; publishes unsubscribe when ZCARD = 0 and agent is streaming.

`test/test_subscriber_sweeper_scheduled_service.py` covers: `subscriber_sweeper` is registered with the taskiq broker; sweeper body removes ZSET members older than the stale threshold and leaves fresh members untouched — for both `workflow_subscribers:*` and `agent_routes:*` key patterns.

`test/test_heartbeat_service.py` covers: `heartbeat()` advances both ZSET scores without adding a new member and without publishing to the agent control channel; heartbeat with no prior `track()` does not raise.

Registry isolation between tests is provided by the `subscriber_registry_isolation` autouse fixture in `app/testing/isolation`. Seed an agent row via `app.testing.seed.seed_agent`.
