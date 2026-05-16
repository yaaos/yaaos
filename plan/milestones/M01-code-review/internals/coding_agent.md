# `domain/coding_agent` — Internal Architecture

> Vendor-neutral abstraction over coding-agent CLIs (Claude Code, Codex, Aider, …). Plugins implement this; consumers (today: `domain/reviewer`) ask for a review or a reply without knowing which CLI is behind it.

## Purpose

`domain/coding_agent` is the contract between yaaof and external agent CLIs. It owns:

- The `CodingAgentPlugin` Protocol — **targeted methods** (`review`, `reply`), not a generic `invoke(prompt, response_model)`.
- `ReviewContext` / `ReplyContext` — the structured inputs.
- `ReviewResult` / `ReplyResult` — the vendor-neutral outputs (findings come back as `vcs.Finding`; the plugin owns whatever intermediate parsing happens internally).
- `InvocationTelemetry`, `InvocationStatus`, `ValidationResult`.
- The plugin registry + top-level dispatch.
- The typed exception hierarchy for infrastructure failures.

Lives in **`domain/`** rather than `core/` because its return types reference `vcs.Finding` and other domain models (`core < domain < plugins < testing`).

It owns **zero prompt assembly** and **zero output-format choice**. Those are plugin concerns — different CLIs need different system prompts, schema-instruction conventions, and tool-allowance flags. Consumers describe *what* they want (review this PR, reply to this comment); the plugin decides *how*.

Yaaof itself makes **no LLM API calls** in M01. The CLI agent (Claude Code) does that internally. `domain/coding_agent` just orchestrates the subprocess via `Workspace.run_coding_agent_cli` (see [workspace.md](workspace.md)).

## Public interface (`__all__`)

```python
# Types
"AgentSpec",
"ReviewContext", "ReplyContext",
"ReviewResult", "ReplyResult",
"InvocationStatus", "InvocationTelemetry",
"ValidationResult", "HealthStatus",

# Protocol
"CodingAgentPlugin",

# Registry + dispatch
"register_coding_agent_plugin", "get_plugin",
"review", "reply",
"validate_config", "health_check_all",

# Exceptions (infrastructure errors only)
"CodingAgentError", "PluginNotFoundError", "CodingAgentCacheMiss",
```

## Types

### `AgentSpec` — the persisted definition of an agent

```python
class AgentSpec(BaseModel):
    name: str                            # e.g., "architecture" / "security" / "style"
    prompt_text: str                     # persona / focus instructions (NOT the full prompt)
    coding_agent_plugin_id: str          # which plugin to dispatch to
    agent_config: dict[str, Any] = {}    # plugin-specific (model, timeout, max_turns)
```

`prompt_text` is the **persona** — focus/role instructions the plugin weaves into its own review prompt. The plugin owns the structural framing (system message, schema appendix, tool allowances); the persona is content.

### `ReviewContext`

```python
class ReviewContext(BaseModel):
    persona: str
    agent_name: str
    pr: VCSPullRequest                     # from domain/vcs
    diff: Diff
    lessons: list[Lesson]                  # from domain/memory
    language_hint: str | None
    prior_yaaof_comment_bodies: list[str]
    agent_config: dict[str, Any]
```

### `ReplyContext`

```python
class ReplyContext(BaseModel):
    persona: str
    agent_name: str
    pr: VCSPullRequest
    diff: Diff
    reply_body: str                        # the human's message
    parent_comment_external_id: str
    agent_config: dict[str, Any]
```

### `InvocationTelemetry` — shared across both result types

```python
class InvocationTelemetry(BaseModel):
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: Decimal | None
    latency_ms: int
    raw_output: str                        # agent response text — for debugging
    raw_stderr: str
```

### `ReviewResult`

```python
class ReviewResult(BaseModel):
    status: InvocationStatus               # SUCCESS / PARSE_FAILURE / AGENT_ERROR / TIMEOUT
    findings: list[Finding]                # vcs.Finding — ready to hand to vcs.post_review
    state: Literal["APPROVED", "CHANGES_REQUESTED", "COMMENT"] | None
    summary_body: str | None
    lesson_ids_consulted: list[UUID]       # for audit
    telemetry: InvocationTelemetry
    error_message: str | None
```

`findings` are already `vcs.Finding`s. Consumers don't convert; they wrap them in a `vcs.Review` and call `vcs_plugin.post_review`.

### `ReplyResult`

```python
class ReplyResult(BaseModel):
    status: InvocationStatus
    body: str | None                       # the agent's reply text
    telemetry: InvocationTelemetry
    error_message: str | None
```

## `CodingAgentPlugin` Protocol

```python
class CodingAgentPlugin(Protocol):
    meta: PluginMeta   # id="claude_code", type="coding_agent", display_name="Claude Code", …
    async def review(self, workspace: Workspace, context: ReviewContext) -> ReviewResult: ...
    async def reply(self, workspace: Workspace, context: ReplyContext) -> ReplyResult: ...
    async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult: ...
    async def health_check(self) -> HealthStatus: ...
```

`review` and `reply` MUST NOT raise on agent-level failures (timeout, non-zero exit, malformed JSON). Those go into `status` + `error_message` so consumers branch on the same surface. Raise only on infrastructure failures the consumer cannot recover from (e.g., `WorkspaceExecError`).

## Registry + dispatch

```python
async def review(plugin_id: str, workspace: Workspace, context: ReviewContext) -> ReviewResult:
    plugin = get_plugin(plugin_id)
    result = await plugin.review(workspace, context)
    log.info("agent.reviewed", plugin_id=plugin_id, status=result.status, findings=len(result.findings), …)
    return result
```

Thin: log telemetry, forward. No retry, no fallback — those are caller policy.

## What `domain/coding_agent` does NOT do

- Does not assemble prompts. Plugins own that.
- Does not define output JSON schemas. Plugins own that.
- Does not retry. Each `review`/`reply` is one attempt; the consumer (reviewer's job runner) decides retry policy.
- Does not stream. M01 is batch only.
- Does not call LLMs. Plugins shell out to CLIs that do.

## Decisions

### 2026-05-16 — Module lives in `domain/`, not `core/`
`ReviewResult.findings` is `list[vcs.Finding]` — a domain type. A `core/` module cannot depend on `domain/`, so this module must be domain-layer. Reviewer and coding_agent are both domain; reviewer depends on coding_agent, not vice versa.

### 2026-05-16 — Protocol exposes targeted `review` / `reply`, not generic `invoke`
Same operations-not-arguments pattern as `Workspace.run_coding_agent_cli`. New capabilities (M02+ implementer needs `implement(ticket)`; later, TODO-resolving agents need `resolve_todos(context)`) arrive as new methods with their own contexts and result types — not as silently-broader scope on a single generic method.
**Why:** generic `invoke(prompt, response_model)` forced every consumer to own prompt assembly + schema choice + DTO-to-domain conversion. Different consumers duplicated the same conventions (or worse, drifted apart). Targeted methods push that work into the plugin where it belongs and keep consumer code small and intent-revealing.

### 2026-05-16 — Findings come back as `vcs.Finding`, not a plugin-specific DTO
The plugin parses the CLI's JSON into a plugin-internal DTO, then converts to `vcs.Finding` before returning. Consumers never see the intermediate.
**Why:** `vcs.Finding` is yaaof's vendor-neutral finding type — already used by `vcs.Review` and `vcs.post_review`. Two parallel types (one for agent output, one for VCS input) would be redundant and drift.

### 2026-05-16 — `prompt_text` on `reviewer_agents` is persona, not the full prompt
Previously, the agent row's `prompt_text` was the entire LLM prompt assembled by reviewer. It's now the persona/focus the plugin weaves in. Existing seeded prompts (architecture / security / style) already read as focus-style content; no data migration needed.

### 2026-05-16 — Failures stay in `status`; raise only for infrastructure
Timeout / non-zero exit / malformed JSON → `InvocationStatus` value + `error_message`. Only `WorkspaceExecError` (binary can't spawn) is raised, since the consumer can't meaningfully recover within a single invocation.
