# apps/agent — internal architecture

> OS-process scheduler + IPC framing + repo clone + Claude Code subprocess manager; zero business logic.

## Orientation

A single Go binary running as two processes: the **supervisor** (long-lived parent — talks to the control plane, owns coordination) and per-checkout **workspace** children (one per repo, doing git/file/Claude Code work in an isolated temp dir). They speak over pipes (newline-framed JSON).

```
Control plane ──Command──> Supervisor ──routes via──> Pool ──spawns/feeds──> Workspace child
     ▲                          │                       │                        │
     └──events/heartbeat────────┘                  Registry                  temp dir + git
                                              (one record per workspace)
```

- **Command** — a unit of work from the control plane, modeled as a `Command` interface with two families: `WorkspaceCommand` (executed in a child against a `WorkspaceOps` capability) and `AgentCommand` (agent-scoped, executed in the supervisor). Adding a kind = a new type implementing the interface. Zero policy — commands carry fully-specified work.
- **Workspace** — a repo checkout with a lifecycle; the **Pool** owns one registry record per workspace (`{state, path, current command, runner}`) and is the single source of truth heartbeat and disk-sweep read. State has two axes: liveness (`Active`/`Defunct`/`Orphaned`) and busy-ness (current command).
- **Agent lifecycle** — after auth the agent is `unconfigured`; its claim declares that state, the control plane sends a `ConfigUpdateCommand`, and the agent becomes `active`, capped at `max_workspaces`. A `ShutdownCommand` transitions to `draining` (new_workspaces=0, pool drain, then self-exit); a `CancelShutdownCommand` returns to `active`. State is held in `Supervisor.localLifecycle` (`atomic.Pointer[string]`).
- **Concurrency** — one root context; the supervisor owns all goroutines; cancel is the only shutdown. **N** (active workspaces) drives process/FD footprint; claim-worker count drives execution width, independent of N.
- **Errors** — command errors become failure events, transport errors retry, only boot-time errors crash, panics are recovered at the worker boundary.
- **Observability** — every log/span/metric carries `org_id` + `agent_id`; local logs are text, OTLP export is structured and late-bound on config.

## Principle

The agent is **zero biz logic**. Every threshold, prompt, lesson, depth, and timeout comes from the control plane via AgentCommand payload. The agent owns: process spawning, IPC framing, repo clone, credential redaction, and telemetry. No policy.

## Layer graph

Imports flow downward only. `depguard` in `apps/agent/.golangci.yml` enforces permitted edges with `list-mode: strict` — every internal package has its own `allow:` rule, so anything not explicitly listed fails CI. Adding a new internal import requires extending the importer's `allow:` list in `.golangci.yml`.

```
supervisor ──→ workspace, command, activity   (+ utilities)
workspace  ──→ command, protocol              (+ ipc, secret, tracing)
command    ──→ protocol                       (+ secret)
activity   ──→ protocol
protocol   (leaf — no internal imports)
utilities  (all leaves: backoff, secret, ipc, logging, observability, identity, tracing)
```

Two test-seam sub-packages are quarantined — depguard forbids non-`_test.go` files from importing them:

```
workspacetest  (internal/workspace/workspacetest/) — leaf; test-only StubHandler
supervisortest (internal/supervisor/supervisortest/) — may import workspace, workspacetest, command, protocol, ipc; test-only InProcessSpawn
```

The key invariant: `protocol` does not import `command`. `ClaimCommand` returns `[]byte`; the supervisor calls `command.Decode` to get a typed `Command`. This keeps the arrow pointing down without a cycle.

## Subcommands

- `agent supervisor` — long-polls [`core/agent_gateway`](../../backend/docs/core_agent_gateway.md), exchanges identity, spawns one OS process per active workspace, heartbeats workspace registry + liveness, runs the disk janitor.
- `agent workspace` — per-workspace child process; reads raw JSON frames over stdin, decodes via `command.Decode`, calls `Execute` on the typed `WorkspaceCommand`, writes AgentEvents over stdout via `ipc` framing.

## Package layout

- `cmd/agent/` — main entrypoint; subcommand dispatch.
- `internal/ipc/` — JSON-newline framing for supervisor↔workspace pipes; partial-read tolerance + concurrency-safe encoder.
- `internal/protocol/` — wire types + HTTP client matching [`apps/backend/openapi/agent-api.yaml`](../../backend/openapi/agent-api.yaml); `openapi_drift_test.go` asserts every property name has a matching `json:` tag. See [protocol.md](protocol.md).
- `internal/command/` — polymorphic `Command` interface, the 7 workspace command types + `ConfigUpdateCommand`, `WorkspaceOps`/`AgentOps` capability seams, typed result structs, and the `Decode` factory. See [command.md](command.md).
- `internal/supervisor/` — identity exchange, N concurrent claim-loop workers, heartbeat loop, per-workspace runner `Pool`; `pool.Dispatch` spawns/reuses/reaps workspace subprocesses. See [supervisor.md](supervisor.md).
- `internal/workspace/` — per-workspace dispatch loop (`Run`); `RealHandler` (production) implements `command.WorkspaceOps`: tempdir lifecycle, clone, write-files, auth-refresh, RunClaude, RunCodex, cleanup. See [workspace.md](workspace.md).
- `internal/workspace/workspacetest/` — test-only `StubHandler`; satisfies `command.WorkspaceOps` with no-op success. Quarantined: depguard forbids non-`_test.go` files from importing it.
- `internal/supervisor/supervisortest/` — test-only `InProcessSpawn`; runs `workspace.Run` in-process via `io.Pipe` pairs so supervisor tests need no OS process. Quarantined: depguard forbids non-`_test.go` files from importing it.
- `internal/tracing/` — OTel wiring; W3C TraceContext propagation; `TraceparentEnv` exports current span to child processes.
- `internal/identity/` — `Provider` interface + `Credentials` struct; `placeholderProvider` carries the signed-STS payload; `Supervisor` depends on the interface for first-exchange and renewal. See [identity.md](identity.md).
- `internal/activity/` — activity WebSocket protocol: `SubscriptionSet`, `WorkspaceMapping`, `Batcher` (250 ms flush), `Conductor`. See [activity.md](activity.md).
- `internal/secret/` — `Secret` type; `String/GoString/MarshalJSON/Format` all return `"[REDACTED]"`; `.Value()` is the explicit unwrap; `UnmarshalJSON` reads the raw string into `s.v` so `map[string]secret.Secret` can be populated via JSON decode (used for `AgentConfig.ApiKeys`).
- `internal/backoff/` — `1m → 3m → 5m → 15m → 60m` schedule with ±20 % jitter; per-surface counters (`sts`, `claim`, `heartbeat`, `ws`). `NewWithStepsAndDeadline` allows custom step lists with the same deadline ceiling (used for the env-tunable STS surface).
- `internal/observability/` — OTel SDK bootstrap, metric instrument declarations, standard dimension helpers (`SetStandardDimensions`, `StandardAttrs`). See [observability.md](observability.md).

## Wire-protocol internals

### Activity WebSocket

Supervisor maintains a bidirectional WS to `/api/v1/agent/activity` when `Config.ActivityWSURL` is set. Agent identity is bearer-derived; no agent ID in the URL. `Batcher` buffers events per subscribed key and flushes one `activity_batch` frame per key at 250 ms. On dial failure the supervisor falls back to per-event HTTP `PostCommandEvent`.

### Live progress streaming

`RealHandler.RunClaude` dispatches via the `RunFunc` seam (production default: `RunStreaming`) and wires `OnStdoutLine` to push each Claude Code stream-json line as a `kind=progress` AgentEvent while also accumulating locally for the terminal event. `progress` events record without resuming the run engine — only `completed_*` events resume it.

### Per-command completion token

Each command header carries a one-time backend-minted `completion_token`. The agent echoes it on every AgentEvent it posts for that command (`received`, `progress`, terminal `completed_*`); the backend verifies it by hash before accepting the event. The token rides the supervisor→child IPC hop via the embedded `CommandHeader` and is threaded onto every constructed `AgentEvent` (workspace terminal + progress, supervisor-synthesized failures, AgentCommand success). The agent never inspects or stores it — pure pass-through. See [protocol.md § Completion token](protocol.md).

### Workspace registry and lifecycle

The `Pool` is the single owner of workspace state. It holds one record per workspace_id; each record tracks two orthogonal axes:

- **Liveness** (`WorkspaceState`): `Active` → subprocess is running; `Defunct` → subprocess exited unexpectedly; `Orphaned` → leftover from a prior run.
- **Busy-ness** (`current_command_id`): `""` when idle, set to the in-flight command_id during `Dispatch`.

Heartbeat `status` is a pure projection of liveness: `Active → "running"`, `Defunct → "exited"`, `Orphaned → "unknown"`. An idle Active workspace reports `status="running"` — the heartbeat does not under-report between commands.

The disk sweep reads `pool.KnownIDs()` — Active + Defunct + Orphaned — so no registered directory is ever removed. The startup scan calls `pool.seedOrphan(id, path)` per found manifest. The forgotten-workspaces janitor reads `pool.Paths()` and calls `pool.remove(id)` after `os.RemoveAll` succeeds.

Full state machine and record shapes → [workspace_lifecycle.md](workspace_lifecycle.md).

### Per-command timeouts

`Pool.Dispatch` wraps each `runner.Send` in `context.WithTimeout` using `cmd.Timeout()`. Each command type owns its deadline: `InvokeClaudeCodeCommand.Timeout()` and `InvokeCodexCommand.Timeout()` both read `Limits.WallclockSeconds` from the wire (falling back to a 15-minute Go-side default); all other kinds use Go-side defaults defined in `internal/command`. On timeout the pool emits `completed_failure` with reason `timeout: <kind> exceeded <duration> wall-clock` and drops the runner so the next `ProvisionWorkspace` can respawn.

### InvokeClaudeCode failure excerpt

On a non-zero claude exit, `RunClaude` returns `claude exit <code>: stderr=<…head…> stdout_tail=<…tail…>` — the supervisor maps this to the `completed_failure` event's `failure_reason`. Both halves ride because claude with `--output-format=stream-json` emits its `{"type":"result","is_error":true,…}` event at the end of stdout, so stderr alone is usually empty. Caps live in `realhandler.go` (`errStderrCap`, `errStdoutTailCap`); excerpts mark truncation explicitly (`...[truncated tail]` on stderr head, `...[truncated head]` on stdout tail). Empty halves render `<empty>` so a missing capture is distinguishable from a captured-nothing.

### Skill-path check + artifact collection + exit-push

`RunClaude` performs three additional steps around every `InvokeClaudeCode` invocation, all in `realhandler.go`:

- **Pre-spawn skill check.** `InvokeClaudeCodeCommand.SkillPath` (backend-computed, convention `.claude/skills/<skill_name>/SKILL.md`) is stat'd inside the checkout before claude is spawned. Empty → `completed_failure` with `failure_reason="skill not found: (empty skill_path)"` (rejected explicitly, before the stat — `filepath.Join(path, "")` resolves to the checkout root, which always exists, so an unchecked empty value would otherwise pass silently). Non-empty but absent from the checkout → `completed_failure` with `failure_reason="skill not found: <path>"`. Either way no subprocess ever launches. Zero agent policy — the convention is entirely backend-side; the agent only stats the path it's given.
- **Artifact collection.** `TMPDIR` is set on the claude subprocess to a workspace-local directory (`workspaceTmpDirName`, torn down by `Cleanup`'s `os.RemoveAll` — no per-file deletion, so a failed run's artifact survives for debugging until then). After the subprocess exits — regardless of its own exit status — `readArtifact` reads `$TMPDIR/<command_id>.md` through `io.LimitReader(f, artifactMaxBytes+1)` (2 MiB cap) rather than a Stat-then-Read pair, so the size cap is enforced atomically with the read instead of racing a file that could grow between a separate stat and read. Three outcomes: file absent → `(nil, "")` (legitimate — review invocations and non-completed main-skill outcomes write none); file present and under cap → `(&body, "")`; file present and over cap → `(nil, "<message>")`, distinguishing "wrote none" from "wrote too much". Both fields ride the terminal `AgentEvent`'s top-level `artifact`/`artifact_error` (not inside `outputs`) via `command.InvokeResult.ArtifactPayload()`, which `workspace.executeCommand` reads on both the success and error return paths — a push failure never masks a real artifact.
- **Exit-push.** `maybePushOriginHead` runs `git push origin HEAD` iff HEAD is a named branch (`headBranchName` via `git symbolic-ref -q --short HEAD`); a detached checkout (today's only real-world case — see `gitClone` above) or a non-repo directory both resolve to "skip, not a failure". A named branch with no new commits is itself a push no-op (git reports "Everything up-to-date"). A genuine push failure (most commonly non-fast-forward) is a stage failure carrying the redacted git stderr — the artifact still ships on that same terminal event.

Mechanical exit order: skill-stat (pre-spawn) → TMPDIR set (pre-spawn) → subprocess runs → artifact read → conditional push → terminal event reported.

### InvokeCodex execution

`RunCodex` in `realhandler.go` mirrors `RunClaude`'s pre/post scaffolding but calls the OpenAI Codex CLI:

- **Pre-spawn skill check.** Same stat logic as `RunClaude` — `InvokeCodexCommand.Proto.SkillPath` (convention `.codex/skills/<skill_name>/SKILL.md`) is stat'd before any subprocess.
- **Output schema.** When `InvokeCodexCommand.Proto.OutputSchemaJSON` is non-empty, `RunCodex` writes it to `$TMPDIR/<command_id>-schema.json` and appends `--output-schema <path>` to the codex argv.
- **Artifact collection + exit-push.** Same mechanics as `RunClaude` — artifact at `$TMPDIR/<command_id>.md`, 2 MiB cap, conditional push via `maybePushOriginHead`.

Credentials are the `CODEX_API_KEY` env var injected by `ExecSpawn` — no per-command secret rides `InvokeCodexCommand`.

### API key credential delivery

Per-org API keys arrive via `ConfigUpdate.AgentConfig.ApiKeys` — a `map[string]secret.Secret` keyed by provider ID. The mapping from provider ID to env var name lives in two identical package-level tables:

- `apiKeyProviderEnvVars` in `internal/workspace/realhandler.go` — used by `RealHandlerConfig.RunClaude` and `RunCodex` when the workspace subprocess is run in-process (test path; `InProcessSpawn`).
- `apiKeyProcessEnvVars` in `internal/supervisor/exec_spawn.go` — used by `ExecSpawn` to inject secrets into the OS subprocess's `cmd.Env` before `exec.Command.Start`. This is the production path.

Currently registered providers and their injected env vars:

| Provider | Env var |
|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openai` | `CODEX_API_KEY` |
| `rwx` | `RWX_ACCESS_TOKEN` |

The backend's `build_api_key_secrets_for_org` forwards **all** stored org keys; the tables above are the agent-side allowlist — unknown providers are ignored.

**rwx CLI binary** — the `rwx` CLI is bundled into the agent Docker image via a dedicated `rwx-downloader` build stage in `apps/agent/Dockerfile`. The stage curls the Linux binary from `github.com/rwx-cloud/rwx/releases` (versioned by `RWX_VERSION` build-arg, default `v3.16.0`), verifies its sha256 against the per-arch digest pinned in the Dockerfile (`RWX_SHA256_AMD64`/`RWX_SHA256_ARM64` build-args — bumped together with `RWX_VERSION`) before executing it, and the runtime stage copies it to `/usr/local/bin/rwx`. `RWX_ACCESS_TOKEN` (injected by `ExecSpawn`) is the credential that authenticates the subprocess to RWX cloud services when executing `rwx` commands.

The production flow: `ExecSpawn` accepts an `apiKeyGetter func() map[string]secret.Secret` parameter. The supervisor passes a closure over `s.config.Load()` (`atomic.Pointer[command.AgentConfig]`) so `apiKeyGetter` always reads the latest applied config — key rotations take effect on the next workspace spawn without restarting the supervisor. Secret values are unwrapped via `.Value()` only at the `cmd.Env = append(cmd.Env, envVar+"="+sec.Value())` site inside `ExecSpawn` — the only point they leave the `secret.Secret` wrapper.

**Two-phase supervisor construction** — `New()` must create the `Supervisor` struct (`s`) before wiring `ExecSpawn`, because the API key getter closure captures `s.config`. The order in `New()`:
1. Detect `needsDefaultSpawn := cfg.Spawn == nil`.
2. Allocate `s := &Supervisor{...}` (without `pool`).
3. If `needsDefaultSpawn`, set `s.cfg.Spawn = ExecSpawn(..., func() { return s.config.Load().ApiKeys })`.
4. Set `s.pool = NewPool(s.cfg.Spawn, log)`.

This avoids a forward-reference: the closure captures the already-allocated `s` pointer, which is safe because no goroutine reads `s.config` until `Run()` is called.

**`secret.Secret.UnmarshalJSON`** — added to `internal/secret/secret.go` so `map[string]secret.Secret` can be populated by `command.Decode` from the `AgentConfigWire.ApiKeys map[string]string` field. Unmarshal reads the raw string value and stores it in `s.v` — never logging the plaintext.

### Credential redaction

All auth tokens flow through `internal/secret.Secret`. `fmt.Sprintf`, `json.Marshal`, and `log.Printf` variants all emit `"[REDACTED]"`; `.Value()` is the only way to unwrap. No token bytes leak into logs or error messages.

## Observability

All three OTel signals (traces, metrics, logs) share two standard dimensions on every record produced after identity exchange: `org_id` and `agent_id`. These are set once via `observability.SetStandardDimensions` immediately after the first successful identity exchange and never change for the process lifetime.

- **Resource attributes** (per OTel signal): `service.name="agent"`, `service.version` (build-stamped), `service.instance.id` (backend-assigned via identity exchange), and `deployment.environment.name` (arrives via ConfigUpdate from backend `Settings.environment`; absent when ConfigUpdate carries an empty value).
- **Span / metric attributes** (post-exchange): `org_id`, `agent_id` — stamped on every span automatically by `DimProcessor` (registered in `observability.wireProviders`); per-span code never sets them explicitly. Per-command spans also carry `workspace_id`, `command_id`, `kind`.
- **Base slog logger**: the supervisor calls `slog.SetDefault(slog.Default().With("org_id", ..., "agent_id", ...))` after first exchange so every subsequent `slog.*` call emits both dimensions automatically.
- **Before ConfigUpdate**: SDK uninstalled — all instruments resolve to no-op providers; agent ships no telemetry. ConfigUpdate is the only install trigger. The agent reads no `OTEL_*` env vars.

`DimProcessor` reads the current dim values at `OnStart` time from the module-level dim store. Pre-identity-exchange spans (e.g. `agent.identity_exchange`) emit without `org_id`/`agent_id` — the processor is a no-op while either value is empty.

**Span inventory** — all spans the agent emits via `tracing.StartSpan`. Every span carries `org_id` + `agent_id` automatically via `DimProcessor` after identity exchange. Each is a child of the span in the "Parent" column (or a root if the context carries no parent):

| Span name | Parent | Where | Notable attributes |
|---|---|---|---|
| `supervisor.dispatch.<kind>` | backend's `agent_command.dispatch.<kind>` span (propagated via the `traceparent` field in the `AgentCommand` wire payload) | `supervisor.go` `routeCommand` | `workspace_id`, `command_id`, `kind`; `run_id` when present |
| `workspace.handle.<kind>` | `supervisor.dispatch.<kind>` | `workspace.go` `executeCommand` | `workspace_id`, `command_id`, `kind`; `run_id` when present |
| `workspace.clone` | `workspace.handle.ProvisionWorkspace` | `realhandler.go` `ProvisionWorkspace` | |
| `workspace.runclaude` | `workspace.handle.InvokeClaudeCode` | `realhandler.go` `RunClaude` | |
| `workspace.runcodex` | `workspace.handle.InvokeCodex` | `realhandler.go` `RunCodex` | |
| `agent.identity_exchange` | inherits caller context; root at current call sites | `supervisor.go` `exchangeIdentity` | |
| `agent.identity_refresh` | inherits caller context; root at current call sites | `supervisor.go` `runOneRefreshCycle` | |
| `agent.claim` | none (per HTTP call, NOT per loop iteration) | `supervisor.go` `claimLoop` | `claim.outcome` (always present): `command` = claim + decode succeeded; `no_command` = HTTP 204; `cancel` = SIGTERM during long-poll; `error` = transport/protocol error or decode failure. `ErrNoCommand` and graceful-shutdown cancel close the span Unset; errors close it Error. |
| `agent.event_post` | the dispatch span that produced the event (`supervisor.dispatch.<kind>` or `workspace.handle.<kind>` ancestor) | `supervisor.go` `postTerminalEvent` | `command_id`, `kind`, `event_post.outcome` (always present — agent perspective: `acked`/`stale_claim`/`network_error`), `command_event.outcome` (only on 200 — backend perspective, e.g. `event_recorded`); one span per HTTP attempt inside the retry loop. HTTP 410 closes the span Unset with `event_post.outcome=stale_claim` and no `command_event.outcome`. See [observability.md § Span attributes](observability.md) for the two-attribute distinction. |
| `agent.activity_ws.dial` | none (per dial attempt, NOT per message) | `supervisor.go` `dialAndStartWS` | |

Grep recipe: `rg -n "tracing.StartSpan" apps/agent/internal/`

Details → [observability.md](observability.md).

## Error handling — fatal-on-mismatch carve-out

The supervisor's normal error model is: retry-with-backoff forever, log warnings, never panic. One exception is carved out: **identity-integrity violations on bearer renewal**.

After first exchange, `supervisor` pins `agentID` and `orgID`. Every subsequent call to `exchangeIdentity` (bearer renewal) must return the same values. If the backend returns different `AgentID` or `OrgID`, `runOneRefreshCycle` returns `fatal=true` and `bearerRefreshLoop` calls `os.Exit(1)`.

Rationale: an agent instance that silently continues operating under a different identity would corrupt org-scoped audit and run records. A hard exit forces the orchestrator to restart with a fresh exchange rather than propagating bad identity silently.

## Concurrency model

The supervisor runs these goroutines concurrently after identity exchange:

- **N claim workers** (`Config.Concurrency`, default 1) — each runs its own `claimLoop`; after claiming and decoding a command, the claim worker calls `Pool.MarkDispatchPending()`. For a `WorkspaceCommand` it spawns a `dispatch` goroutine and immediately re-arms for the next claim. For an `AgentCommand` (e.g. `ConfigUpdate`) it runs `dispatch` **inline** so the side effect (`ApplyConfig` storing the config pointer) is visible to the next `buildClaimRequest` — without this the loop would re-arm while `s.config.Load()` is still nil and the next claim would go out with `Lifecycle="unconfigured"`, over-claiming additional pinned ConfigUpdates during boot. Workers share the `Pool` (mutex-guarded) and the `protocol.Client` (inherently safe). `buildClaimRequest` uses a 1-second short poll when `Pool.PendingDispatch() > 0` — preventing a 30-second stall before the goroutine's Pool.Dispatch registers the workspace in the pool. The Pool owns this pending-dispatch count, so all claim-capacity facts (`ActiveIDs`, `IdleIDs`, `PendingDispatch`) come from one source.
- **Dispatch goroutines** (one per claimed `WorkspaceCommand`) — own `postReceivedEvent` + `routeCommand` + `postTerminalEvent`; run a `defer recover()` that converts panics to `completed_failure` events; call `Pool.MarkDispatchSettled()` (via `defer`) after `routeCommand` returns. Not in `Run()`'s WaitGroup; abandoned best-effort on shutdown (backend failsafe owns in-flight recovery). `AgentCommand` dispatch runs on the claim worker itself, not a spawned goroutine.
- **Heartbeat loop** — fires on `Config.HeartbeatInterval` (default 30 s); reads `Pool.Snapshot()` under the pool's lock.
- **Bearer refresh loop** — wakes ~1 h before bearer expiry; calls `exchangeIdentity` and `client.SetBearer` (atomic store).
- **Disk sweep loop** — fires every 5 min; reads `Pool.KnownIDs()` under the pool's lock; calls `os.RemoveAll` for orphan dirs.
- **Activity WS read loop** (optional) — runs while the WS is connected; exits on transport error, triggering the reconnect loop.
- **WS reconnect loop** (optional) — waits on `wsReadLoopDone`, sleeps on the WS backoff schedule, re-dials.

No goroutine shares mutable state without a lock or atomic. The `Pool` guards all workspace-record mutations with a `sync.Mutex`. `Conductor.SubscriptionSet` and `WorkspaceMapping` each have their own independent locks. `observability.SetStandardDimensions` is guarded by `stdDimsMu`.

**Backoff is env-tunable on two independent surfaces.** Both take a comma-separated list of positive integers (seconds, e.g. `2,2,2,2,2`) parsed by the shared `parseBackoffSeconds`; unset or malformed → a WARN and a fall back to the prod ramp (`1m/3m/5m/15m/60m`).

- `YAAOS_AGENT_STS_BACKOFF_SECONDS` overrides the STS identity-exchange step list. The 1 h deadline cap applies regardless of the step list.
- `YAAOS_AGENT_OPS_BACKOFF_SECONDS` overrides the operational surfaces — `claimBackoff`, `heartbeatBackoff`, and `wsBackoff`. These are **indefinite** (no deadline cap): a transient blip must not kill a running pod. The env is parsed once at `supervisor.New`; each surface gets its own schedule, so a malformed value WARNs once, not three times.

**Mid-command re-auth.** A 401/403 response on a terminal-event post (in `postTerminalEvent`) triggers `reauthIfUnauthorized` before retrying — identical to the claim-loop and heartbeat paths. The `reauthMu` serializes concurrent re-auth attempts across all goroutines; a goroutine that loses the TryLock falls back to the normal backoff sleep and retries after the winner has updated the shared bearer. Error classification (`classifyConnErr`) matches both the numeric HTTP codes (`: 401 `, `: 403 `) and the text form returned by `ClaimCommand` and `doJSON` (`: unauthorized`).

**`YAAOS_AGENT_ACCEPT_IDENTITY_CHANGE=1`.** Test-only env var, honored **only in `-tags agent_test` builds**: lets the agent accept a different `agent_id`/`org_id` after a DB wipe (e.g. `resetStack()` in the e2e suite). The acceptance decision is the single `acceptIdentityChange()` seam — two files split on the `agent_test` build tag (`identity_seam_off.go` returns `false` unconditionally; `identity_seam_on.go` reads the env var). The production binary compiles the off variant, so it has no code path that reads the env var and **cannot** be configured to continue under a changed identity. Both reauth surfaces — `reauthIfUnauthorized` and the scheduled `runOneRefreshCycle` — route through the seam, so the rule is identical on both. The e2e agent container builds with the `agent_test` tag (`BUILD_TAGS` arg in `apps/agent/Dockerfile`, set in `docker/docker-compose.test.yml`).

**`YAAOS_AGENT_AUDIENCE_OVERRIDE`.** Dev-only env var, honored **only in `-tags agent_dev` builds**: overrides the STS claim audience that `exchangeIdentity` would otherwise derive from `hostFromURL(BaseURL)`. Needed in local dev stacks where the agent reaches the backend over an internal Docker service name (e.g. `web:8080`) but the backend's `YAAOS_PUBLIC_ORIGIN` is a host-mapped address (e.g. `localhost:8080`) so browser-facing OAuth/email links resolve from the developer's machine. The override is the single `audienceOverride()` seam — two files split on the `agent_dev` build tag (`audience_seam_off.go` returns `""` unconditionally; `audience_seam_on.go` reads the env var). The production binary compiles the off variant and has no code path that reads the env var. The dev compose sets `BUILD_TAGS: agent_dev` and `YAAOS_AGENT_AUDIENCE_OVERRIDE: localhost:8080`; the e2e compose is unaffected (it already aligns both sides via `YAAOS_PUBLIC_ORIGIN: http://web:8080`).

## Testing model

Tests are pure-stdlib and fake-driven at the capability seams (`WorkspaceOps`, `AgentOps`, `identity.Provider`, `CloneFunc`, `RunFunc`); timing tests run in a `testing/synctest` bubble; every concurrency invariant ships a `-race` test (reviewer-gated convention); `protocol/openapi_drift_test.go` is the cross-plane Go↔Python schema-parity guard. Full per-layer map → [patterns.md § Testing](patterns.md#testing).
