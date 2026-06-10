# internal/observability

> Wires the WorkspaceAgent's OpenTelemetry SDK and declares the standard metric/span dimensions.

## Scope

- **Owns:** `Init` (SDK bootstrap from env vars — traces, metrics, logs), `BindExporter` (late-binds the OTLP exporter from a ConfigUpdate endpoint), `SetInstanceID` (stores the backend-assigned instance_id before `BindExporter` runs), `Instruments` (metric instruments), `SetStandardDimensions`/`StandardAttrs` (org_id + agent_id on metrics), `bindMetrics` (instrument resolution).
- **Does not own:** the base slog logger (owned by `internal/logging`), span creation (owned by `internal/tracing`), or identity exchange (owned by `internal/identity` + `internal/supervisor`).
- **Receives:** `Config{ServiceVersion}` at startup; `instance_id` after identity exchange via `SetInstanceID`; `(orgID, agentID)` pair after identity exchange via `SetStandardDimensions`.
- **Emits:** OTel resource + SDK providers wired into the global `otel.*` registries; `Result.SlogHandler` for the logging fan-out.

## Standard dimensions

Every signal carries two kinds of attributes:

- **Resource attributes** (static for the process lifetime, set before BindExporter):
  - `service.name` = `yaaos-workspace-agent`
  - `service.version` = binary version — sourced from `main.agentVersion` (ldflags-injectable `var`, defaults to `"0.0.0-dev"`); `YAAOS_AGENT_VERSION` env wins at runtime. Both the OTel `Init` call and `supervisor.Config.Version` read the same `envOr("YAAOS_AGENT_VERSION", agentVersion)` expression — there is no split-brain fallback.
  - `service.instance.id` = `instance_id` — the backend-assigned role-session-name from the STS ARN (`workspace_agents.instance_id`). Correlates OTel signals to a specific `workspace_agents` row. Empty until set via `SetInstanceID` after identity exchange; populated before `BindExporter` runs in the normal ConfigUpdate path.
- **Span / metric attributes** (set after identity exchange):
  - `org_id` — the org this agent instance belongs to; pinned on first identity exchange.
  - `agent_id` — the `workspace_agents` row PK; pinned on first identity exchange.

`instance_id` is resource-only because it belongs to the OTel resource model and is stable for the process lifetime. `org_id` and `agent_id` are span/metric attributes because they're assigned by the backend; they appear after `SetStandardDimensions` is called from the supervisor.

## Resource vs attribute split — why

OTel resources describe the emitting entity (the agent instance). Span/metric attributes describe the event. Putting `org_id`/`agent_id` on the resource would require rebuilding the SDK before those values are known; attaching them as attributes avoids that. Cardinality is safe: orgs and agents are few.

## Ordering: SetInstanceID before BindExporter

The normal production flow:

1. `Init` runs at startup — no OTLP endpoint yet, no-op SDK.
2. Identity exchange completes → supervisor calls `SetInstanceID(resp.InstanceID)`.
3. ConfigUpdate arrives → supervisor calls `BindExporter(endpoint, ...)`.
4. `BindExporter` calls `buildResource(startupCfg)` which includes the stored `instance_id`.

The env-var path (OTLP endpoint set at startup via `OTEL_EXPORTER_OTLP_ENDPOINT`) installs the providers from `Init`. In that case `instance_id` is empty in the OTel resource; `SetInstanceID` stores it in `startupCfg` but `BindExporter` is a no-op (already installed). The env-var path is not the production path — the ConfigUpdate path is.

## Local vs OTLP output

The OTLP endpoint arrives by one of two paths; both install the same SDK providers (traces, metrics, logs) and genuinely export.

- **Env-var path** (`OTEL_EXPORTER_OTLP_ENDPOINT` set at startup): `Init` constructs the OTLP/HTTP exporters from the standard `OTEL_EXPORTER_OTLP_*` env vars and wires the providers immediately.
- **ConfigUpdate path** (no env endpoint at startup): `Init` is a no-op — instruments resolve through the SDK no-op provider, `Metrics()` call sites work without nil-checking, no goroutines start. When a later `ConfigUpdateCommand` carries an `otlp_endpoint`, `BindExporter` constructs the exporters against that endpoint (`WithEndpointURL`; an `otlp_token`, when present, rides as a `Bearer` Authorization header) and installs the providers — late-binding the live pipeline. The supervisor calls it from `ApplyConfig`.
- **Idempotent install**: `BindExporter` is a no-op when the endpoint is empty or the providers are already installed (env path already ran, or a prior ConfigUpdate already bound) — the pipeline is installed at most once.
- **Log fan-out is late-bindable**: the logging fan-out is frozen at `logging.Init`, so `Init` always hands back a live log bridge (`Result.SlogHandler`) that `main` wires in once at startup. The bridge drops records until a logger provider exists, then delegates to it — so logs export on the ConfigUpdate path, not just traces and metrics. See `logbridge.go`.

Either path: customers configure their own collector (Datadog, Honeycomb, etc.) downstream; the agent speaks OTLP/HTTP only.

## Per-command dimensions

The supervisor adds `workspace_id` and `command_id` as span attributes on the `supervisor.dispatch.<kind>` span for each command (see `internal/supervisor`). These are span-scoped, not process-wide.

## Instruments summary

Key counters emitted by the supervisor (all carry `org_id` + `agent_id`):

| Instrument | Extra attributes | Meaning |
|---|---|---|
| `yaaos.agent.commands.deduped` | — | Duplicate `command_id` hit the dedup cache; no re-execution |
| `yaaos.agent.events.post.retries` | `kind` | Each retry of a terminal-event POST (transient failure) |
| `yaaos.agent.commands.completed` | `result` | Terminal dispatch outcome (success / failure / timeout) |
| `yaaos.agent.connection.failures` | `surface`, `class` | Auth or network failures per connection surface |

## Gotchas

- `bindMetrics` is called from `Init` after the real provider installs, swapping out no-op instruments. Tests that call `Metrics()` before `Init` get no-ops — fine for unit tests; service tests need the real provider only if they assert metric values.
- `SetStandardDimensions` is safe to call concurrently (guarded by `stdDimsMu`); it's a process-wide singleton, called once after identity exchange.

## Testing

- `otel_test.go` uses a real `httptest.Server` as the OTLP receiver; its polling loops wait on real HTTP export and cannot run in `testing/synctest` bubbles (OTLP SDK goroutines block on OS network I/O). See [patterns.md § Testing](patterns.md) principle 6 for the general rule.

## Entry points

- `apps/agent/internal/observability/otel.go` — `Init`, `Config`, `Result`, `SetInstanceID`.
- `apps/agent/internal/observability/metrics.go` — `Instruments`, `Metrics()`, `SetStandardDimensions`, `StandardAttrs`.
- `apps/agent/cmd/agent/main.go` — `var agentVersion` (ldflags target `main.agentVersion`; `YAAOS_AGENT_VERSION` runtime override).
- `apps/agent/VERSION` — human-edited major integer; the publish pipeline derives the full semver from it.
