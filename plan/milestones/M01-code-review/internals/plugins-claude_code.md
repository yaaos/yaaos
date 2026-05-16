# `plugins/claude_code` — Internal Architecture

> First concrete `domain/coding_agent` plugin. Wraps the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) as a subprocess.

## Purpose

`plugins/claude_code` is yaaof's adapter for Claude Code, the only coding-agent CLI we support in M01. It:

- Implements every method on `CodingAgentPlugin` (`review`, `reply`, `validate_config`, `health_check`).
- **Owns prompt assembly** — system framing, persona injection, diff/lessons/comments sections, JSON-schema appendix. Domain consumers (today: reviewer) describe what they want via `ReviewContext` / `ReplyContext`; the plugin assembles the actual LLM prompt internally.
- **Owns the output schema** — defines plugin-internal `_FindingDto`, `_FindingList`, `_ReplyResponse` Pydantic models, asks the agent to emit JSON matching them, then converts to vendor-neutral `vcs.Finding` before returning. Consumers never see plugin internals.
- Builds the CLI command line and environment, then hands them to `workspace.run_coding_agent_cli(argv, env, stdin, timeout_seconds)`. The workspace owns subprocess spawning, process-group timeout enforcement, and where the CLI actually runs (host tempdir today; future Docker container).
- Parses the CLI wrapper envelope (`{result, usage, total_cost_usd}`) and the agent's JSON response into `ReviewResult` / `ReplyResult`.
- Owns the `claude_code_settings` table (encrypted Anthropic API key + CLI config).
- Computes the review verdict (`APPROVED` / `CHANGES_REQUESTED` / `COMMENT`) from the findings the agent emits.
- **Owns its own HTTP routes** under `/api/claude_code/`:
  - `POST /api/claude_code/api_key` — sets / rotates the Anthropic key (encrypted at rest via `core/config.yaaof_encryption_key`).
  - `GET /api/claude_code/health` — wraps `ClaudeCodePlugin.health_check()`; returns `{healthy, message, checked_at}`.

  Wired via `apps/backend/app/plugins/claude_code/web.py` with `register_routes(RouteSpec(module_name="claude_code", router=router))`. Per [backend.md § 2026-05-16 Plugin-owned URL namespaces](../backend.md#decisions), plugin credentials and health checks live in the plugin's own URL space — not under `/api/settings/`.

No knowledge of yaaof tickets, review_jobs, audit log, or the workspace's internal `working_dir`.

## Public interface (`__all__`)

```python
"ClaudeCodePlugin",   # the CodingAgentPlugin instance, registered at bootstrap
```

Everything else is internal. Domain code never imports from this plugin; it uses `domain/coding_agent`'s registry.

## Settings table

`claude_code_settings` (see [data-model.md](../data-model.md)):

- `encrypted_anthropic_api_key` — passed to subprocess as `ANTHROPIC_API_KEY` at invoke time.
- `default_model` — optional override for the model.
- `cli_path` — optional override for the `claude` binary location.
- `default_timeout_seconds` — wall-clock cap (default 600).

Decrypted at plugin construction; held in plugin singleton state. Plugin re-reads if it can't find creds in memory (e.g., admin updated mid-run).

## `review()` and `reply()` implementations

```python
async def review(self, workspace: Workspace, context: ReviewContext) -> ReviewResult: ...
async def reply(self, workspace: Workspace, context: ReplyContext) -> ReplyResult: ...
```

Both share the same machinery; review/reply differ only in (a) the assembled prompt content and (b) the JSON output schema (`_FindingList` vs `_ReplyResponse`). The shared steps:

### Step 1: assemble the prompt + append the schema instruction

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

### Step 3: prepare environment + run via workspace

```python
env = os.environ.copy()
env["ANTHROPIC_API_KEY"] = self._decrypted_anthropic_key

timeout = agent_config.get("timeout_seconds") or settings.default_timeout_seconds

result = await workspace.run_coding_agent_cli(
    argv=[
        cli_path, "--print", "--output-format=json",
        "--permission-mode=bypassPermissions",
        "--allowed-tools=Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch",
        *model_flags,
    ],
    env=env,
    stdin=full_prompt.encode("utf-8"),
    timeout_seconds=timeout,
)
```

The workspace owns subprocess lifecycle: it spawns the CLI with whatever `cwd` (or container, in future plugins) corresponds to the repo checkout, enforces the timeout via SIGTERM → grace → SIGKILL of the process group, and returns a `CodingAgentCliResult` with `stdout`, `stderr`, `exit_code`, `timed_out`, `duration_ms`.

### Step 4: handle timeout / non-zero exit

```python
latency_ms = result.duration_ms

if result.timed_out:
    return AgentInvocationResult(
        status=AgentInvocationStatus.TIMEOUT,
        latency_ms=latency_ms,
        error_message=f"claude did not return within {timeout}s",
    )

if result.exit_code != 0:
    return AgentInvocationResult(
        status=AgentInvocationStatus.AGENT_ERROR,
        raw_output=result.stdout,
        raw_stderr=result.stderr,
        latency_ms=latency_ms,
        error_message=f"claude exited {result.exit_code}: {_first_line(result.stderr)}",
    )
```

### Step 5: parse the wrapper envelope

```python
try:
    envelope = json.loads(result.stdout)
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

### Step 6: strict-parse the agent's response against the plugin-internal model

```python
# review path: schema = _FindingList; reply path: schema = _ReplyResponse
try:
    parsed_dict = json.loads(agent_text)
    parsed = _FindingList.model_validate(parsed_dict)
except (json.JSONDecodeError, ValidationError) as e:
    return ReviewResult(
        status=InvocationStatus.PARSE_FAILURE,
        telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
        error_message=f"agent response didn't match _FindingList: {e}",
    )

return ReviewResult(
    status=InvocationStatus.SUCCESS,
    findings=[_dto_to_finding(f) for f in parsed.findings],   # → vcs.Finding
    state=_compute_state(parsed.findings),                    # APPROVED / CHANGES_REQUESTED / COMMENT
    summary_body=None,
    lesson_ids_consulted=[l.id for l in context.lessons],
    telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
)
```

The plugin-internal `_FindingDto` → `vcs.Finding` conversion happens here, before the result leaves the plugin. Consumers never see `_FindingDto`.

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

Owned by the workspace, not this plugin. `in_process_workspace.run_coding_agent_cli` spawns with `start_new_session=True`, then on timeout: `os.killpg(SIGTERM)` → wait 2s → `os.killpg(SIGKILL)`. This plugin only reads `result.timed_out` and `result.exit_code` and never sees a subprocess directly.

## Concurrency

Plugin is a singleton; many concurrent `review()` / `reply()` calls are expected (different workspaces, different agents). Each call:

- Spawns its own subprocess (no shared state).
- Reads the decrypted API key from plugin state (immutable after construction; safe under concurrency).
- Maintains no per-call instance state.

No locks needed for the common path.

## Audit-side captures

`domain/coding_agent.review()` / `reply()` emit a structured log line per call; the plugin populates the fields it knows from the wrapper envelope (returned in `ReviewResult.telemetry` / `ReplyResult.telemetry`). The plugin does NOT write to `audit_log` directly — that's reviewer's job (with full domain context: ticket_id, review_job_id, agent_id, prompt hash).

## What `plugins/claude_code` does NOT do

- Does not build the caller's prompt — reviewer does. Plugin only appends the schema instruction.
- Does not retry. Each `review()` / `reply()` is one attempt. Reviewer marks the review_job `failed` if needed.
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
`json.loads()` of the agent's response → validate against the plugin-internal Pydantic model (`_FindingList` for review, `_ReplyResponse` for reply). On any failure → `PARSE_FAILURE` with raw output captured in `telemetry.raw_output`. The schema-instruction prompt is the contract.
**Why:** deterministic parsing. Fallback heuristics mask non-compliance; better to fail visibly and tune the prompt than to silently coax.

### 2026-05-15 — Prompt delivered via stdin (not CLI arg)
Avoids `ARG_MAX` limits on big diffs. Cleaner than temp files.

### 2026-05-16 — Subprocess spawning moved out of this plugin into the workspace
Previously this plugin called `asyncio.create_subprocess_exec` with `cwd=workspace.working_dir`. Now it calls `workspace.run_coding_agent_cli(argv, env, stdin, timeout_seconds)`. The workspace owns where/how the process runs; this plugin owns what to run and how to interpret the output.
**Why:** `working_dir` is a host-filesystem concept that doesn't generalize to future Docker / K8s workspaces. Operations-not-paths gives those plugins room to spawn inside containers without breaking this plugin. Subprocess timeout + process-group semantics are also a workspace concern (they're identical for any coding-agent CLI), not an agent-vendor concern.

### 2026-05-15 — `health_check()` does not call Anthropic
Verifies CLI installed + API key set, nothing more. Real API connectivity is checked only when an invocation happens.
**Why:** health_check runs frequently (dashboard onboarding status); avoid token spend on probes.

### 2026-05-16 — Plugin owns prompt assembly and the output JSON schema
Previously `domain/reviewer/prompt.py` assembled the LLM prompt and `domain/reviewer/finding_types.py` defined the response Pydantic models, which were then passed to `core.coding_agent.invoke(prompt, response_model)`. Now both live in this plugin (`_assemble_review_prompt`, `_assemble_reply_prompt`, `_FindingDto`, `_FindingList`, `_ReplyResponse`, `_schema_appendix`). The plugin parses agent output into its internal `_FindingDto` and converts to `vcs.Finding` before returning. Consumers never see the intermediate shape.
**Why:** different coding-agent CLIs (Codex, Aider) will need different framing conventions, different schema-instruction styles, and different tool-allowance flags. Pushing that into the plugin keeps consumer code agent-agnostic — reviewer just builds a `ReviewContext` and asks for findings — and forces each new plugin author to think explicitly about how their CLI should be prompted.
