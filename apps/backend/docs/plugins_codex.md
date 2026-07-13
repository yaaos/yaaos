# plugins/codex

> `CodingAgentPlugin` implementation wrapping the OpenAI Codex CLI (`@openai/codex`).

## Scope

Registers one `CodingAgentPlugin` with `plugin_id="codex"`, `command_kind="InvokeCodex"`. Owns skill-path convention (`.codex/skills/<skill_name>/SKILL.md`), Codex-specific argv/env construction, JSONL event parsing, OpenAI API key validation, static model/effort defaults, and the codex-native skills-bundle renderer.

Does NOT own: workspace mechanics, agent dispatch, run-lifecycle tables, or API key storage — those are `core/coding_agent` and `core/api_keys`.

## Public interface

- `CodexPlugin` — the `CodingAgentPlugin` implementation; registered at import time via `bootstrap()`.
- Bootstrap side effects: `register_plugin(CodexPlugin())` + `api_keys.register_validator("openai", validate_openai_key)` + `register_command_hydrator("InvokeCodex", _codex_command_hydrator)`. See § Credential gate and hydrator below.

## Module architecture

### Core user flows

1. **Skill invocation.** `compile_invocation(invocation)` builds `InvokeCodingAgent` with `argv=["codex", "--model", model, "--quiet", skill_path]`, empty env, empty stdin. `build_command` separately reads `context["output_schema"]` and stamps `InvokeCodexCommand.output_schema_json` — the vendor-neutral `InvokeCodingAgent` carries no schema field. The Go agent writes it as `$TMPDIR/<command_id>-schema.json` and appends `--output-schema <path>` to argv before spawning.
2. **Command construction.** `build_command(*, compiled, invocation, build, session)` — called by `core/coding_agent.dispatch_invocation` for every codex dispatch — gates on an org-level OpenAI API key via `_require_org_openai_key` (raises `CredentialUnavailableError` when absent), derives `output_schema_json` from `invocation.context["output_schema"]` (str passthrough / dict → `json.dumps` / absent → `None` — the sole place this normalization happens), and returns the wire `InvokeCodexCommand`. See [core_coding_agent.md § `CodingAgentPlugin` Protocol](core_coding_agent.md#codingagentplugin-protocol).
3. **Result parsing.** `parse_result(terminal_event_payload)` reads `stdout` from the terminal AgentEvent outputs; parses JSONL looking for `item.completed` (role=`assistant`) events — the last such event's first `output_text` item is the result text. A `turn.completed` event carries `usage`.
4. **Activity streaming.** `parse_activity_line(line)` decodes one JSONL frame: `item.completed` (assistant_message role) → `ActivityEvent(kind="assistant_message", message=<text>)`; `turn.completed` → `ActivityEvent(kind="result", message="turn completed")`; unrecognized or blank → `None`.
5. **Settings validation.** `validate_settings(settings)` accepts no keys — Codex has no per-org auth setting, the only credential source is the org-level OpenAI API key (`core/api_keys`). Any key raises `ValueError`.
6. **API key validation.** `validate_openai_key(key: SecretStr) -> bool` posts `GET https://api.openai.com/v1/models` with the key as bearer token; returns `True` on 200, `False` on 401/403, re-raises on other errors.
7. **Skills-bundle rendering.** `render_skill_bundle(skills, agents)` produces a codex-native bundle layout:
   - Each skill → `.codex/skills/<name>/SKILL.md` (reconstructed markdown with frontmatter) + extra files (`.claude/` prefix remapped to `.codex/`).
   - Each agent → `.codex/agents/<name>.toml` (TOML with `name`, `description`, and `[prompt].content` as a TOML literal multi-line string). The prompt body prepends a defensive restatement directive ("Before taking any action, restate the specific deliverable from the task you received") then appends the original agent body. Any `'''` in the body is replaced with `'' '` to avoid breaking the TOML literal-string delimiter.
   - `AGENTS.md` at the repo root — contains the delegation-authorization sentence required by the codex multi-agent protocol: "these applicable AGENTS.md instructions explicitly authorize sub-agents, delegation, and parallel agent work".

### Entities

None — the plugin is stateless; all state lives in `core/coding_agent` tables.

## Data owned

No tables. No persistent state.

## Credential gate and hydrator

Two functions registered at bootstrap (well — `_codex_command_hydrator` is registered at bootstrap; the credential gate lives inside `build_command` and is called directly), both api_key-only — Codex has no per-user credential path:

**`_require_org_openai_key(org_id, session) -> None`** — the dispatch-time gate: checks `core/api_keys` for an org-level `"openai"` key, raises `CredentialUnavailableError("No OpenAI API key …")` if absent. Called directly by `CodexPlugin.build_command` before constructing the wire `InvokeCodexCommand` — `core/coding_agent.dispatch_invocation` calls `build_command` for every codex dispatch, so this gate fires on the real dispatch path.

**`_codex_command_hydrator(payload, ctx, session) -> dict`** — claim-time hydrator called by `core/agent_gateway` on every `InvokeCodex` command when it's claimed. Returns the payload unchanged — the Go agent reads `CODEX_API_KEY` from the ConfigUpdate `api_keys` map, so no claim-time credential injection is needed.

## How it's tested

- `app/plugins/codex/test/test_parse_result_method.py` — `CodexPlugin.parse_result` unit.
- `app/plugins/codex/test/test_parse_activity_line.py` — `CodexPlugin.parse_activity_line` unit.
- `app/plugins/codex/test/test_validate_settings.py` — `CodexPlugin.validate_settings` unit: empty settings accepted, any key (including a stale `auth_mode`) rejected as unknown.
- `app/core/coding_agent/test/test_skills_bundle.py` (codex renderer tests) — paths emit `.codex/` prefix; `AGENTS.md` contains delegation-authorization vocabulary; agent TOMLs include defensive restatement + correct TOML structure.
- `app/plugins/codex/test/test_claim_hydrator_service.py` (`@pytest.mark.service`) — `_codex_command_hydrator`: payload returned unchanged, no `auth_json` added.
- `app/plugins/codex/test/test_build_command_service.py` (`@pytest.mark.service`) — `CodexPlugin.build_command`: no key → `CredentialUnavailableError`; with key → `InvokeCodexCommand` with envelope fields from the `CommandBuildContext` and `limits.wallclock_seconds` from the compiled exec block; `output_schema_json` normalization (str passthrough / dict → `json.dumps` / absent → `None`) from `invocation.context["output_schema"]`.
- `app/core/coding_agent/test/test_dispatch_invocation_service.py` — service: a real `CodexPlugin` dispatch with no org OpenAI key raises `CredentialUnavailableError` through the full `dispatch_invocation` path.
