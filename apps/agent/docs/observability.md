# internal/observability

> Wires the WorkspaceAgent's OpenTelemetry SDK and declares the standard metric/span dimensions.

## Scope

- **Owns:** `Init` (SDK bootstrap — wires live log bridge, stashes startup Config), `BindExporter` (installs OTLP exporters from a ConfigUpdate endpoint), `SetInstanceID` (stores the backend-assigned instance_id before `BindExporter` runs), `Instruments` (metric instruments), `SetStandardDimensions`/`StandardAttrs` (org_id + agent_id on metrics), `bindMetrics` (instrument resolution), `RebindMetrics` (low-level atomic re-bind hook after a MeterProvider swap; tests go through `observabilitytest.InstallTestMeterProvider`).
- **Does not own:** the base slog logger (owned by `internal/logging`), span creation (owned by `internal/tracing`), or identity exchange (owned by `internal/identity` + `internal/supervisor`).
- **Receives:** `Config{ServiceVersion}` at startup; `instance_id` after identity exchange via `SetInstanceID`; `(orgID, agentID)` pair after identity exchange via `SetStandardDimensions`.
- **Emits:** OTel resource + SDK providers wired into the global `otel.*` registries; `Result.SlogHandler` for the logging fan-out.

## Standard dimensions

Every signal carries two kinds of attributes:

- **Resource attributes** (static for the process lifetime, set before BindExporter):
  - `service.name` = `agent`
  - `service.version` = binary version — sourced from `main.agentVersion` (ldflags-injectable `var`, defaults to `"0.0.0-dev"`); `YAAOS_AGENT_VERSION` env wins at runtime. Both the OTel `Init` call and `supervisor.Config.Version` read the same `envOr("YAAOS_AGENT_VERSION", agentVersion)` expression — there is no split-brain fallback.
  - `service.instance.id` = `instance_id` — the backend-assigned role-session-name from the STS ARN (`workspace_agents.instance_id`). Correlates OTel signals to a specific `workspace_agents` row. Empty until set via `SetInstanceID` after identity exchange; populated before `BindExporter` runs in the normal ConfigUpdate path.
  - `deployment.environment.name` = the OTel deployment environment (e.g. `local`, `staging`, `production`). Arrives via ConfigUpdate from backend `Settings.environment`. Absent from the resource when ConfigUpdate carries an empty value — no explicit `""` tag is ever stamped.
- **Span / metric attributes** (set after identity exchange):
  - `org_id` — the org this agent instance belongs to; pinned on first identity exchange.
  - `agent_id` — the `workspace_agents` row PK; pinned on first identity exchange.

`instance_id` is resource-only because it belongs to the OTel resource model and is stable for the process lifetime. `org_id` and `agent_id` are span/metric attributes because they're assigned by the backend; they appear after `SetStandardDimensions` is called from the supervisor.

`org_id` and `agent_id` are stamped on every span automatically by `DimProcessor` (registered in `wireProviders`). Per-span code never needs to set them explicitly — the processor reads the current dim values at `OnStart` time, so a mutation between two spans is reflected immediately. Pre-identity-exchange spans (e.g. `agent.identity_exchange`) emit without these attributes; `DimProcessor` is a no-op while either value is empty.

## Resource vs attribute split — why

OTel resources describe the emitting entity (the agent instance). Span/metric attributes describe the event. Putting `org_id`/`agent_id` on the resource would require rebuilding the SDK before those values are known; attaching them as attributes avoids that. Cardinality is safe: orgs and agents are few.

## Ordering: SetInstanceID before BindExporter

The normal production flow:

1. `Init` runs at startup — no OTLP endpoint yet, providers stay no-op.
2. Identity exchange completes → supervisor calls `SetInstanceID(resp.InstanceID)`.
3. ConfigUpdate arrives → supervisor calls `BindExporter(endpoint, ...)`.
4. `BindExporter` calls `buildResource(startupCfg)` which includes the stored `instance_id`.

## OTLP install path

OTel providers are uninstalled at boot — all instruments resolve to no-op providers
and the agent ships no telemetry. When the first `ConfigUpdate` arrives with a
non-empty `otlp_endpoint`, `BindExporter` constructs the OTLP/HTTP trace/metric/log
exporters, points them at `endpoint`, attaches a `Bearer` Authorization header
when `otlp_token` is non-empty, and installs the SDK providers globally. The live
log bridge's delegate swaps to the new log provider, so logs start flowing too.

There is no env-var startup path. The agent reads no `OTEL_*` env vars. Every
operational telemetry knob arrives from the control plane via `ConfigUpdate`.

`SetInstanceID` is called after identity exchange and before the first `ConfigUpdate`,
so the resource carries `service.instance.id` when providers install.

## Per-command dimensions

The supervisor adds `workspace_id`, `command_id`, `kind`, and (when present) `run_id` as span attributes on the `supervisor.dispatch.<kind>` span for each command. The workspace child adds the same attributes to the `workspace.handle.<kind>` span. These are span-scoped, not process-wide. `org_id` and `agent_id` are no longer set explicitly on these spans — `DimProcessor` handles them automatically.

## Instruments summary

Key counters emitted by the supervisor (all carry `org_id` + `agent_id`):

| Instrument | Extra attributes | Meaning |
|---|---|---|
| `yaaos.agent.commands.deduped` | — | Duplicate `command_id` hit the dedup cache; no re-execution |
| `yaaos.agent.events.post.retries` | `kind` | Each retry of a terminal-event POST (transient failure) |
| `yaaos.agent.commands.completed` | `result` | Terminal dispatch outcome (success / failure / timeout) |
| `yaaos.agent.connection.failures` | `surface`, `class` | Auth or network failures per connection surface |
| `yaaos.agent.claim.outcome` | `outcome` | Claim-loop exit-path bucket (see below); supplements `yaaos.agent.commands.claimed` |

### `yaaos.agent.claim.outcome` buckets

`outcome` attribute values:

| Value | Meaning |
|---|---|
| `command` | Claim HTTP call succeeded **and** `command.Decode` succeeded; a command was dispatched. Equivalent path to an increment of `yaaos.agent.commands.claimed`. |
| `no_command` | HTTP 204 — normal long-poll outcome when the backend has no queued command. |
| `cancel` | `err != nil && ctx.Err() != nil` — transport error coincident with a cancelled root context (SIGTERM during a long-poll). |
| `error` | `err != nil && ctx.Err() == nil` (transport/protocol error not caused by shutdown), **or** a decode failure after a successful HTTP claim. |

`yaaos.agent.commands.claimed` is preserved; it continues incrementing on the `command` path and may be referenced by existing dashboards. `yaaos.agent.claim.outcome` is the labeled superset — use it to write `outcome="error"` alerts without touching existing queries.

## Span attributes added by this module

### `agent.claim` span

- `claim.outcome` (string) — set on every span to one of `command`, `no_command`, `cancel`, `error`. Set before `endClaim` so the attribute is always present regardless of span status.

### `agent.event_post` span

Two outcome attributes coexist — they describe different perspectives:

- `event_post.outcome` (string, **agent perspective** — new) — set on every attempt:
  - `acked` — backend returned 200.
  - `stale_claim` — backend returned 410 (retired command row); not an error.
  - `network_error` — transient or auth error; the attempt will be retried.
- `command_event.outcome` (string, **backend perspective** — existing) — set only on 200, carries the backend's classification (`event_recorded`). Absent on non-200 spans (a stale claim is a 410 with no ack body, surfaced via `event_post.outcome="stale_claim"`).

Use `event_post.outcome="network_error"` for SRE alerting on transient delivery failures (set a threshold, not alert-on-any). Use `event_post.outcome="stale_claim"` to alert on retired-row drops (zero in steady state — any hit is paging-worthy).

## Gotchas

- `bindMetrics` is called from `Init` after the real provider installs, swapping out no-op instruments. Tests that call `Metrics()` before `Init` get no-ops — fine for unit tests; service tests need the real provider only if they assert metric values.
- `metricsRef` is an `atomic.Pointer[Instruments]`: `bindMetrics` atomically stores a fresh set and `Metrics()` atomically loads, so a re-bind (production `BindExporter` at config time, or a test provider swap) is race-free against concurrent claim/heartbeat-loop readers. `RebindMetrics()` is the exported low-level hook for that re-bind; tests go through `observabilitytest.InstallTestMeterProvider` rather than calling it plus `otel.SetMeterProvider` by hand.
- `SetStandardDimensions` is safe to call concurrently (guarded by `stdDimsMu`); it's a process-wide singleton, called once after identity exchange.

## Testing

- `otel_test.go` uses a real `httptest.Server` as the OTLP receiver; its polling loops wait on real HTTP export and cannot run in `testing/synctest` bubbles (OTLP SDK goroutines block on OS network I/O). See [patterns.md § Testing](patterns.md) principle 6 for the general rule.

## Entry points

- `apps/agent/internal/observability/otel.go` — `Init`, `Config`, `Result`, `SetInstanceID`, `wireProviders` (registers `DimProcessor`).
- `apps/agent/internal/observability/dim_processor.go` — `DimProcessor`, `NewDimProcessor`.
- `apps/agent/internal/observability/metrics.go` — `Instruments`, `Metrics()`, `RebindMetrics()`, `SetStandardDimensions`, `StandardAttrs`.
- `apps/agent/internal/observability/observabilitytest/provider.go` — test-only seam: `InstallTestMeterProvider`, `MetricCapture.CounterSums`. Quarantined (depguard forbids non-test files importing it).
- `apps/agent/cmd/agent/main.go` — `var agentVersion` (ldflags target `main.agentVersion`; `YAAOS_AGENT_VERSION` runtime override).
- `apps/agent/VERSION` — human-edited major integer; the publish pipeline derives the full semver from it.
