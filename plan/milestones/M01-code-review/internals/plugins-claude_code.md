# `plugins/claude_code` — Internal Architecture

> First concrete `core/coding_agent` plugin. Wraps the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) as a subprocess.

## Purpose

`plugins/claude_code` is yaaof's adapter for Claude Code, the only coding-agent CLI we support in M01. It:

- Implements every method on `CodingAgentPlugin` (`invoke`, `validate_config`, `health_check`).
- Manages CLI invocation: builds the command line, sets environment variables, spawns the subprocess in the workspace directory, captures output, parses results.
- Owns the `claude_code_settings` table (encrypted Anthropic API key + CLI config).

No business logic about review. No knowledge of yaaof tickets, agents, or workspaces beyond the `Workspace` Protocol surface.

## Public interface (`__all__`)

```python
"ClaudeCodePlugin",   # the CodingAgentPlugin instance, registered at bootstrap
```

Everything else is internal. Domain code never imports from this plugin; it uses `core/coding_agent`'s registry.

## Settings table

`claude_code_settings` (see [data-model.md](../data-model.md)):

- `encrypted_anthropic_api_key` — passed to subprocess as `ANTHROPIC_API_KEY` at invoke time.
- `default_model` — optional override for the model.
- `cli_path` — optional override for the `claude` binary location.
- `default_timeout_seconds` — wall-clock cap (default 600).

Decrypted at plugin construction; held in plugin singleton state. Plugin re-reads if it can't find creds in memory (e.g., admin updated mid-run).

## `invoke()` implementation

```python
async def invoke(
    self,
    workspace: Workspace,
    prompt: str,
    agent_config: dict[str, Any],
    response_model: type[BaseModel],
) -> AgentInvocationResult[T]:
```

### Step 1: append the schema instruction to the prompt

```
<caller's prompt>

## Output Format (STRICT)

Respond with EXACTLY a JSON object matching this schema. No markdown fences.
No commentary. No preamble. Your response must start with `{` and end with `}`.

<json.dumps(response_model.model_json_schema(), indent=2)>
```

This is the *only* mechanism to constrain output shape. Claude Code's `--output-format=json` flag controls the *wrapper* envelope, not the agent's content.

### Step 2: build the CLI command

```
claude
  --print                                  # non-interactive batch
  --output-format=json                     # wrapper as JSON (gives us usage stats)
  --permission-mode=bypassPermissions      # headless; auto-accept allowed tools
  --allowed-tools=Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch
  [--model=<override>]                     # if agent_config["model"] or settings.default_model
  [--max-turns=<n>]                        # if agent_config["max_turns"]
  --                                       # end of flags
  <prompt>                                 # the full assembled prompt (passed via stdin to avoid shell-arg length limits)
```

Prompt is piped via stdin (Claude Code accepts `claude --print` with no positional arg and reads the prompt from stdin). This avoids `ARG_MAX` limits that big diffs would otherwise hit if the prompt were passed as a CLI argument.

### Step 3: prepare environment + spawn subprocess

```python
env = os.environ.copy()
env["ANTHROPIC_API_KEY"] = self._decrypted_anthropic_key
# remove any inherited interactive-only variables
for var in ["ANTHROPIC_LOG", "CLAUDE_CONFIG_DIR", "TERM"]:
    env.pop(var, None)

timeout = agent_config.get("timeout_seconds") or settings.default_timeout_seconds

proc = await asyncio.create_subprocess_exec(
    cli_path, "--print", "--output-format=json",
    "--permission-mode=bypassPermissions",
    "--allowed-tools=Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch",
    *model_flags,
    cwd=workspace.working_dir,
    env=env,
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    process_group=0,    # new process group so we can kill children together
)
```

### Step 4: write prompt to stdin, wait with timeout

```python
start = time.monotonic()
try:
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(input=prompt.encode("utf-8")),
        timeout=timeout,
    )
except asyncio.TimeoutError:
    # kill process group (the CLI + any child processes it spawned)
    os.killpg(proc.pid, signal.SIGTERM)
    await asyncio.sleep(2)
    if proc.returncode is None:
        os.killpg(proc.pid, signal.SIGKILL)
    return AgentInvocationResult(
        status=AgentInvocationStatus.TIMEOUT,
        parsed=None,
        raw_output="",
        raw_stderr="",
        latency_ms=int((time.monotonic() - start) * 1000),
        error_message=f"Claude Code did not return within {timeout}s",
        ...
    )

latency_ms = int((time.monotonic() - start) * 1000)
```

### Step 5: handle non-zero exit

```python
if proc.returncode != 0:
    return AgentInvocationResult(
        status=AgentInvocationStatus.AGENT_ERROR,
        parsed=None,
        raw_output=stdout.decode("utf-8", errors="replace"),
        raw_stderr=stderr.decode("utf-8", errors="replace"),
        latency_ms=latency_ms,
        error_message=f"claude exited {proc.returncode}: {_first_line(stderr)}",
        ...
    )
```

### Step 6: parse the wrapper envelope

```python
try:
    envelope = json.loads(stdout)
    agent_result_text = envelope["result"]              # the agent's response content
    usage = envelope.get("usage", {})
    tokens_in = usage.get("input_tokens")
    tokens_out = usage.get("output_tokens")
    cost_usd = envelope.get("total_cost_usd")           # if reported
except (json.JSONDecodeError, KeyError) as e:
    # The wrapper itself didn't parse — this is unusual. Treat as AGENT_ERROR.
    return AgentInvocationResult(
        status=AgentInvocationStatus.AGENT_ERROR,
        parsed=None,
        raw_output=stdout.decode("utf-8", errors="replace"),
        raw_stderr=stderr.decode("utf-8", errors="replace"),
        latency_ms=latency_ms,
        error_message=f"Could not parse Claude Code's output envelope: {e}",
        ...
    )
```

### Step 7: strict-parse the agent's response against `response_model`

```python
try:
    parsed_dict = json.loads(agent_result_text)
    parsed = response_model.model_validate(parsed_dict)
except (json.JSONDecodeError, ValidationError) as e:
    return AgentInvocationResult(
        status=AgentInvocationStatus.PARSE_FAILURE,
        parsed=None,
        raw_output=agent_result_text,           # the agent's response text, for debugging
        raw_stderr=stderr.decode("utf-8", errors="replace"),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        error_message=f"Agent response didn't match {response_model.__name__}: {e}",
        ...
    )

return AgentInvocationResult(
    status=AgentInvocationStatus.SUCCESS,
    parsed=parsed,
    raw_output=agent_result_text,
    raw_stderr=stderr.decode("utf-8", errors="replace"),
    tokens_in=tokens_in,
    tokens_out=tokens_out,
    cost_usd=cost_usd,
    latency_ms=latency_ms,
    error_message=None,
)
```

## `validate_config()` implementation

```python
async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult:
    errors = []
    if "timeout_seconds" in agent_config:
        if not isinstance(agent_config["timeout_seconds"], int) or agent_config["timeout_seconds"] <= 0:
            errors.append("timeout_seconds must be a positive int")
    if "max_turns" in agent_config:
        if not isinstance(agent_config["max_turns"], int) or agent_config["max_turns"] <= 0:
            errors.append("max_turns must be a positive int")
    if "model" in agent_config:
        # POC: don't try to enumerate valid model ids (Anthropic ships new ones often).
        # Just check it's a non-empty string.
        if not isinstance(agent_config["model"], str) or not agent_config["model"]:
            errors.append("model must be a non-empty string")
    unknown_keys = set(agent_config) - {"timeout_seconds", "max_turns", "model"}
    for k in unknown_keys:
        errors.append(f"unknown config key: {k}")
    return ValidationResult(valid=not errors, errors=errors)
```

## `health_check()` implementation

```python
async def health_check(self) -> HealthStatus:
    if not self._decrypted_anthropic_key:
        return HealthStatus(provider_id=self.plugin_id, ok=False, detail="anthropic api key not set", ...)
    cli_path = self._cli_path()
    if not cli_path:
        return HealthStatus(provider_id=self.plugin_id, ok=False, detail="claude binary not found on PATH or in settings.cli_path", ...)
    try:
        proc = await asyncio.create_subprocess_exec(
            cli_path, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
        if proc.returncode == 0:
            return HealthStatus(provider_id=self.plugin_id, ok=True, detail=None, ...)
        return HealthStatus(provider_id=self.plugin_id, ok=False, detail=f"claude --version exited {proc.returncode}", ...)
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
        return HealthStatus(provider_id=self.plugin_id, ok=False, detail=str(e), ...)
```

Does NOT make an Anthropic API call. Just verifies the CLI is installed + creds are set.

## CLI path discovery

1. If `claude_code_settings.cli_path` is set → use it.
2. Else: look up `claude` on PATH via `shutil.which("claude")`.
3. If neither: `health_check` returns ok=false; `invoke` raises `CodingAgentError("claude binary not found")`.

## Subprocess cleanup

- `process_group=0` (new pgid for the subprocess) so timeout kill hits the CLI + any children it spawned.
- On timeout: `SIGTERM` → wait 2s → `SIGKILL` if still alive.
- On cancellation (asyncio.Task cancelled while waiting on `communicate`): same kill sequence in the `__aexit__` of the subprocess context (we wrap `proc.communicate` in try/finally for this).

## Concurrency

Plugin is a singleton; many concurrent `invoke()` calls are expected (different workspaces, different agents). Each call:

- Spawns its own subprocess (no shared state).
- Reads the decrypted API key from plugin state (immutable after construction; safe under concurrency).
- Maintains no per-call instance state.

No locks needed for the common path.

## Audit-side captures

`core/coding_agent.invoke()` emits a structured log line; the plugin populates the fields it knows from the wrapper envelope. The plugin does NOT write to `audit_log` directly — that's reviewer's job (with full domain context: ticket_id, review_job_id, agent_id, prompt snapshot).

## What `plugins/claude_code` does NOT do

- Does not build the caller's prompt — reviewer does. Plugin only appends the schema instruction.
- Does not retry. Each `invoke()` is one attempt. Reviewer marks the review_job `failed` if needed.
- Does not stream output. M01 batch only.
- Does not enforce content policy on what tools the agent uses — the `--allowed-tools` flag is the boundary.
- Does not parse PARSE_FAILURE outputs for partial findings. If the agent didn't comply with the schema, we discard and audit. (Future: retry with a re-prompt that includes the failed output and asks for correction. Not in M01.)

## Open questions for implementation

- **Stdin vs CLI-arg for the prompt.** Need to verify Claude Code accepts the prompt on stdin when `--print` is used without a positional arg. If not, write prompt to temp file inside workspace and pass path. Implementation-time detail; both approaches work.
- **Token usage location in the wrapper.** Need to verify the exact JSON keys (`usage.input_tokens` vs `input_tokens` at top level, etc.). Implementation-time; check Claude Code's actual output format.
- **`total_cost_usd` reliability.** Not all Claude Code invocations report it (depends on model + version). Plugin extracts when present; None otherwise.
- **Workspace's git state during agent run.** The agent could (with the allowed tools) call `git log` via... actually no, `Bash` isn't allowed. So no git operations. Reads operate on the working-tree files, which are at the checkout sha.

## Decisions

### 2026-05-15 — All read-only Claude Code tools allowed; bypassPermissions mode
`--allowed-tools=Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch`. `--permission-mode=bypassPermissions` so the CLI accepts these without interactive prompts. Write/edit/Bash tools are NOT allowed in M01.
**Why:** read + agent-internal tracking is the right surface for review. Web tools (`WebFetch`, `WebSearch`) help with library docs / CVE lookups for security review.

### 2026-05-15 — Strict JSON parsing of agent output; no markdown-fence fallback
`json.loads()` of the agent's response → validate against `response_model`. On any failure → `PARSE_FAILURE` with raw output captured. The schema-instruction prompt is the contract.
**Why:** deterministic parsing. Fallback heuristics mask non-compliance; better to fail visibly and tune the prompt than to silently coax.

### 2026-05-15 — Prompt delivered via stdin (not CLI arg)
Avoids `ARG_MAX` limits on big diffs. Cleaner than temp files.

### 2026-05-15 — Plugin runs subprocess in its own process group
Timeout kills hit the CLI + any children it spawned. Prevents orphaned subprocesses if Claude Code shells out internally (it shouldn't, but defense-in-depth).

### 2026-05-15 — `health_check()` does not call Anthropic
Verifies CLI installed + API key set, nothing more. Real API connectivity is checked only when an invocation happens.
**Why:** health_check runs frequently (dashboard onboarding status); avoid token spend on probes.
