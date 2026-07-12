# plugins/codex

> `CodingAgentPlugin` implementation wrapping the OpenAI Codex CLI (`@openai/codex`).

## Scope

Registers one `CodingAgentPlugin` with `plugin_id="codex"`, `command_kind="InvokeCodex"`. Owns skill-path convention (`.codex/skills/<skill_name>/SKILL.md`), Codex-specific argv/env construction, JSONL event parsing, OpenAI API key validation, and static model/effort defaults.

Does NOT own: workspace mechanics, agent dispatch, run-lifecycle tables, or API key storage — those are `core/coding_agent` and `core/api_keys`.

## Public interface

- `CodexPlugin` — the `CodingAgentPlugin` implementation; registered at import time via `bootstrap()`.
- Bootstrap side effect: `register_plugin(CodexPlugin())` + `api_keys.register_validator("openai", validate_openai_key)`.

## Module architecture

### Core user flows

1. **Skill invocation.** `compile_invocation(invocation)` builds `InvokeCodingAgent` with `argv=["codex", "--model", model, "--quiet", skill_path]`, empty env, empty stdin. The `output_schema_json` field flows through `InvokeCodingAgent` → `InvokeCodexCommand.OutputSchemaJSON`; the Go agent writes it as `$TMPDIR/<command_id>-schema.json` and appends `--output-schema <path>` to argv before spawning.
2. **Result parsing.** `parse_result(terminal_event_payload)` reads `stdout` from the terminal AgentEvent outputs; parses JSONL looking for `item.completed` (role=`assistant`) events — the last such event's first `output_text` item is the result text. A `turn.completed` event carries `usage`.
3. **Activity streaming.** `parse_activity_line(line)` decodes one JSONL frame: `item.completed` (assistant_message role) → `ActivityEvent(kind="assistant_message", message=<text>)`; `turn.completed` → `ActivityEvent(kind="result", message="turn completed")`; unrecognized or blank → `None`.
4. **Settings validation.** `validate_settings(settings)` accepts an empty dict; rejects any unknown keys with `ValueError`.
5. **API key validation.** `validate_openai_key(key: SecretStr) -> bool` posts `GET https://api.openai.com/v1/models` with the key as bearer token; returns `True` on 200, `False` on 401/403, re-raises on other errors.

### Entities

None — the plugin is stateless; all state lives in `core/coding_agent` tables.

## Data owned

No tables. No persistent state.

## How it's tested

- `app/plugins/codex/test/test_parse_result_method.py` — `CodexPlugin.parse_result` unit: `item.completed` → output text extracted; usage from `turn.completed`; missing fields return empty.
- `app/plugins/codex/test/test_parse_activity_line.py` — `CodexPlugin.parse_activity_line` unit: assistant_message → `kind="assistant_message"`; `turn.completed` → `kind="result"`; blank/unrecognized → `None`.
- `app/plugins/codex/test/test_validate_settings.py` — `CodexPlugin.validate_settings` unit: empty dict accepted; unknown key raises `ValueError`.
