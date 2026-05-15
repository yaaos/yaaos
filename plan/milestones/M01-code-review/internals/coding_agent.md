# `core/coding_agent` — Internal Architecture

> Vendor-neutral abstraction over coding-agent CLIs (Claude Code, Codex, Aider, …). Plugins implement this; consumers invoke an agent without knowing which CLI is behind it.

## Purpose

`core/coding_agent` is the contract between yaaof and external agent CLIs. It owns:

- `AgentSpec` — the invocable description of an agent.
- `AgentInvocationResult` — what an invocation produces.
- The `CodingAgentPlugin` Protocol every plugin implements.
- The plugin registry + top-level dispatch.
- The typed exception hierarchy for infrastructure failures.

It owns **zero** business logic — no concept of review verdicts, findings, lessons, or what a "review agent" is. Pure infrastructure for "invoke an agent CLI, get structured output back."

Yaaof itself makes **no LLM API calls** in M01. The CLI agent (Claude Code) does all of that internally. `core/coding_agent` just orchestrates the subprocess.

## Public interface (`__all__`)

```python
# Types
"AgentSpec",
"AgentInvocationResult",
"AgentInvocationStatus",
"ValidationResult",
"HealthStatus",

# Protocol
"CodingAgentPlugin",

# Registry + dispatch
"register_coding_agent_plugin",
"get_plugin",
"invoke",
"validate_config",
"health_check_all",

# Exceptions (infrastructure errors only)
"CodingAgentError",
"PluginNotFoundError",
```

## Types

### `AgentSpec`

```python
class AgentSpec(BaseModel):
    name: str                            # e.g., "architecture" / "security" / "style"
    prompt_text: str                     # the system instructions for this agent
    coding_agent_plugin_id: str          # "claude_code" in M01
    agent_config: dict[str, Any] = {}    # plugin-specific options (timeout, model override, etc.)
```

Per-workflow modules subclass this to add persistence fields. M01: `domain/reviewer.ReviewerAgent` (id, org_id, is_built_in, timestamps). M02+: `domain/implementer.ImplementerAgent` will follow the same pattern.

### `AgentInvocationStatus`

```python
class AgentInvocationStatus(StrEnum):
    SUCCESS = "success"               # invocation ran + output parsed cleanly
    PARSE_FAILURE = "parse_failure"   # CLI exited 0 but output didn't match the response_model
    AGENT_ERROR = "agent_error"       # CLI exited non-zero or reported an error
    TIMEOUT = "timeout"               # CLI exceeded the configured wall-clock cap
```

### `AgentInvocationResult`

```python
T = TypeVar("T", bound=BaseModel)

class AgentInvocationResult(BaseModel, Generic[T]):
    status: AgentInvocationStatus
    parsed: T | None                  # populated iff status=SUCCESS
    raw_output: str                   # full CLI stdout; captured for audit + debugging
    raw_stderr: str                   # CLI stderr; populated on errors
    tokens_in: int | None             # best-effort; only if CLI reports
    tokens_out: int | None            # best-effort; only if CLI reports
    cost_usd: Decimal | None          # best-effort; only if CLI reports
    latency_ms: int                   # always populated
    error_message: str | None         # populated for non-SUCCESS; one-line human description
```

**Known outcomes are status values, not exceptions.** Callers pattern-match on `status`. Reviewer marks the review_job `failed` / `posted` etc. based on the status.

### `ValidationResult`

```python
class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = []         # human-readable; empty when valid
```

Returned by `validate_config`. Used by the workflow module that owns its agents (M01: `domain/reviewer`) to reject malformed configs at save time.

### `HealthStatus`

```python
class HealthStatus(BaseModel):
    healthy: bool
    message: str = ""        # short human-readable description ("ok", "claude binary not found", "ANTHROPIC_API_KEY not set")
    checked_at: datetime
```

Returned by each plugin's `health_check()` and aggregated by `health_check_all()`. Used by `domain/settings` to power the onboarding-status dashboard.

## `CodingAgentPlugin` Protocol

```python
class CodingAgentPlugin(Protocol):
    plugin_id: str   # "claude_code", "codex", "aider", etc.

    async def invoke(
        self,
        workspace: Workspace,
        prompt: str,                       # caller's prompt (system + diff + lessons + …)
        agent_config: dict[str, Any],      # from the AgentSpec
        response_model: type[BaseModel],   # the Pydantic class we want output parsed into
    ) -> AgentInvocationResult[T]:
        """Spawn the CLI in workspace.working_dir; wait for completion; parse stdout
        against response_model; return a result.

        Implementation responsibilities:
        - Append a schema instruction to the prompt so the CLI knows what JSON to emit.
          (e.g., "Output JSON matching this schema: <json_schema_serialized>".)
        - Pass agent_config-derived flags to the CLI (timeout, model override, etc.).
        - Set environment variables required by the CLI (e.g., ANTHROPIC_API_KEY for Claude Code)
          from the plugin's own settings table.
        - Enforce the wall-clock cap (either from agent_config.timeout_seconds or plugin default).
          On exceeded: kill subprocess; return status=TIMEOUT.
        - Capture stdout + stderr + exit code.
        - Extract JSON from stdout (CLIs often wrap it in markdown fences or preamble — plugin
          handles that).
        - Validate JSON against response_model; on failure return status=PARSE_FAILURE with
          raw_output for debugging.
        - On non-zero exit: status=AGENT_ERROR with stderr in error_message.
        - On success: status=SUCCESS with parsed model.
        - Capture token usage / cost from CLI output where possible (Claude Code reports this).
          When the CLI doesn't, leave fields None.

        Should NEVER raise unless infrastructure fails (subprocess won't spawn, config table
        unreadable, etc.). All known outcomes are status values."""

    async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult:
        """Validate plugin-specific config at agent-save time.
        Examples for claude_code: timeout_seconds is positive int, model_override is a known
        Anthropic model id, max_turns is reasonable.

        Returns ValidationResult; never raises (validation failures are values, not exceptions)."""

    async def health_check(self) -> HealthStatus:
        """Cheap check that the CLI is installed and credentials are valid.
        For claude_code: check `claude --version` exits 0 and the configured ANTHROPIC_API_KEY
        isn't empty. Does NOT make an Anthropic API call."""
```

## Plugin registry

```python
# core/coding_agent/registry.py
_PLUGINS: dict[str, CodingAgentPlugin] = {}

def register_coding_agent_plugin(plugin: CodingAgentPlugin) -> None:
    if plugin.plugin_id in _PLUGINS:
        raise ValueError(f"Plugin {plugin.plugin_id} already registered")
    _PLUGINS[plugin.plugin_id] = plugin

def get_plugin(plugin_id: str) -> CodingAgentPlugin:
    try:
        return _PLUGINS[plugin_id]
    except KeyError:
        raise PluginNotFoundError(plugin_id) from None
```

## Top-level dispatch

```python
async def invoke(
    plugin_id: str,
    workspace: Workspace,
    prompt: str,
    agent_config: dict[str, Any],
    response_model: type[BaseModel],
) -> AgentInvocationResult[T]:
    """Look up plugin → delegate. Emits a structured log line on completion
    (latency, status, tokens if reported). Does NOT write audit_entries —
    callers (reviewer) attach domain context."""

async def validate_config(plugin_id: str, agent_config: dict[str, Any]) -> ValidationResult:
    """Delegated to the plugin. Called by the workflow that owns its agents (M01: domain/reviewer) on agent save."""

async def health_check_all() -> dict[str, HealthStatus]:
    """Calls health_check() on every registered plugin. Used by domain/settings
    for onboarding status."""
```

## Plugin lifecycle

- Singleton per process. Plugin instantiated at bootstrap.
- Plugin reads its own settings table (e.g., `claude_code_settings`) on construction to populate credentials + defaults.
- Registers itself into `_PLUGINS` via `register_coding_agent_plugin`.
- No per-invocation instance creation; `invoke()` is called concurrently for different workspaces.

## Error contract

Status values cover **known outcomes**:

- CLI ran but output is bad → `PARSE_FAILURE`
- CLI exited non-zero → `AGENT_ERROR`
- CLI hung → `TIMEOUT`
- CLI ran cleanly + output parsed → `SUCCESS`

Exceptions are reserved for **infrastructure failures**:

- Plugin not found (caller passed wrong `plugin_id`) → `PluginNotFoundError`
- Subprocess can't be spawned (CLI binary missing) → `CodingAgentError` (with cause)
- Settings table read fails → underlying DB exception propagates

Reviewer code reads the status field. Only catches `CodingAgentError` (and `PluginNotFoundError`) for the rare infrastructure paths.

## Logging vs audit

`core/coding_agent.invoke()` emits a structured log line per call:

```
{
  "kind": "agent.invoked",
  "plugin_id": "claude_code",
  "agent_name": <from AgentSpec, passed in via agent_config or sidechannel>,
  "status": "success",
  "tokens_in": 1234,
  "tokens_out": 567,
  "cost_usd": 0.0123,
  "latency_ms": 18200
}
```

Drives metrics. Callers (`reviewer`) write the audit_entries row with full domain context (ticket_id, review_job_id, agent_id, the prompt sent, the parsed findings).

## What `core/coding_agent` does NOT do

- Does not build the prompt — callers (`reviewer`) compose it.
- Does not own agent definitions — workflow modules do (M01: `domain/reviewer` owns `reviewer_agents`).
- Does not own credential storage — each plugin has its own settings table.
- Does not retry on failure — that's the caller's decision (reviewer marks the review_job `failed`, the per-PR queue discipline handles re-triggering on new commits).
- Does not stream output. M01 batch only. (M02+ may add streaming for progress UI.)
- Does not write to the database or `audit_log`. Stateless from yaaof's perspective.
- Does not call LLMs directly. The CLI agent does that internally.

## Open questions for implementation

- **Streaming progress**: Claude Code supports streaming. For an M02+ progress UI, we'd add a `stream_invoke()` method. Not in M01.
- **Concurrent invocations of the same plugin**: a single plugin instance handles many concurrent `invoke()` calls (different workspaces). The plugin must be safe under concurrency — usually trivial since each call spawns its own subprocess with no shared mutable state.
- **Cancellation**: in M01 cancellation is cooperative — the calling coroutine (in `domain/reviewer`) polls its `review_jobs.status` at safe points and returns before reaching `invoke()` once superseded/cancelled. Once `invoke()` has started, it runs to completion (or its own timeout). For M02+ implementer agents (hours-long), the long-running invocation supervisor will need real mid-invoke cancellation; that adds a cancel signal to the plugin Protocol and per-plugin subprocess-kill logic.
- **Token usage extraction reliability**: Claude Code reports tokens in its final JSON output; other CLIs may not. Plugins do best-effort extraction. Missing fields are None — metrics tolerate that.

## Decisions

### 2026-05-15 — Known outcomes are status values, not exceptions
`AgentInvocationResult` carries a `status` enum (success / parse_failure / agent_error / timeout). Caller pattern-matches; no try/except for the common failure modes.
**Why:** invocation outcomes are data, not exceptional events. Exception hierarchy is reserved for infrastructure failures (plugin missing, subprocess can't spawn).

### 2026-05-15 — Plugin appends the response-schema instruction to the prompt
Caller passes `response_model: type[BaseModel]`; plugin serializes to JSON Schema and appends "Output JSON matching: …" or uses the CLI's native structured-output flag if available.
**Why:** prompt-formatting conventions vary per CLI. Centralizing in the plugin keeps callers portable across plugins.

### 2026-05-15 — `validate_config()` on the Protocol
Plugin offers config validation. The workflow module that owns its agents (M01: `domain/reviewer`) calls it on agent save; invalid configs are rejected at edit time, not at first-review failure.
**Why:** silent misconfigs surface as cryptic agent errors later. Catching at save time costs little and helps a lot.

### 2026-05-15 — Plugin singleton; concurrent invocations are the norm
One plugin instance per process; many concurrent `invoke()` calls; each spawns its own subprocess. Plugin must be safe under concurrency (trivial for our use case).
