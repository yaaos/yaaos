# plugins/codex

> `CodingAgentPlugin` implementation wrapping the OpenAI Codex CLI (`@openai/codex`).

## Scope

Registers one `CodingAgentPlugin` with `plugin_id="codex"`, `command_kind="InvokeCodex"`. Owns skill-path convention (`.codex/skills/<skill_name>/SKILL.md`), Codex-specific argv/env construction, JSONL event parsing, OpenAI API key validation, static model/effort defaults, and the codex-native skills-bundle renderer.

Does NOT own: workspace mechanics, agent dispatch, run-lifecycle tables, or API key storage — those are `core/coding_agent` and `core/api_keys`.

## Public interface

- `CodexPlugin` — the `CodingAgentPlugin` implementation; registered at import time via `bootstrap()`.
- `build_auth_json(credential: UserOAuthCredential) -> SecretStr` — builds the `chatgptAuthTokens` JSON payload used by the Codex CLI's `CODEX_HOME/auth.json`. Returns a `SecretStr` so the plaintext never appears in logs.
- Bootstrap side effects: `register_plugin(CodexPlugin())` + `api_keys.register_validator("openai", validate_openai_key)` + `register_command_hydrator("InvokeCodex", _codex_command_hydrator)` + `register_user_oauth_app(UserOAuthApp(...))`. The `InvokeCodex` hydrator raises `CredentialHydrationError` for per-user credential flows (not yet supported in this mode); passes through unchanged for API-key mode (credentials arrive via `ConfigUpdate` `api_keys`).

## Module architecture

### Core user flows

1. **Skill invocation.** `compile_invocation(invocation)` builds `InvokeCodingAgent` with `argv=["codex", "--model", model, "--quiet", skill_path]`, empty env, empty stdin. The `output_schema_json` field flows through `InvokeCodingAgent` → `InvokeCodexCommand.OutputSchemaJSON`; the Go agent writes it as `$TMPDIR/<command_id>-schema.json` and appends `--output-schema <path>` to argv before spawning.
2. **Result parsing.** `parse_result(terminal_event_payload)` reads `stdout` from the terminal AgentEvent outputs; parses JSONL looking for `item.completed` (role=`assistant`) events — the last such event's first `output_text` item is the result text. A `turn.completed` event carries `usage`.
3. **Activity streaming.** `parse_activity_line(line)` decodes one JSONL frame: `item.completed` (assistant_message role) → `ActivityEvent(kind="assistant_message", message=<text>)`; `turn.completed` → `ActivityEvent(kind="result", message="turn completed")`; unrecognized or blank → `None`.
4. **Settings validation.** `validate_settings(settings)` accepts an empty dict; rejects any unknown keys with `ValueError`.
5. **API key validation.** `validate_openai_key(key: SecretStr) -> bool` posts `GET https://api.openai.com/v1/models` with the key as bearer token; returns `True` on 200, `False` on 401/403, re-raises on other errors.
6. **Skills-bundle rendering.** `render_skill_bundle(skills, agents)` produces a codex-native bundle layout:
   - Each skill → `.codex/skills/<name>/SKILL.md` (reconstructed markdown with frontmatter) + extra files (`.claude/` prefix remapped to `.codex/`).
   - Each agent → `.codex/agents/<name>.toml` (TOML with `name`, `description`, and `[prompt].content` as a TOML literal multi-line string). The prompt body prepends a defensive restatement directive ("Before taking any action, restate the specific deliverable from the task you received") then appends the original agent body. Any `'''` in the body is replaced with `'' '` to avoid breaking the TOML literal-string delimiter.
   - `AGENTS.md` at the repo root — contains the delegation-authorization sentence required by the codex multi-agent protocol: "these applicable AGENTS.md instructions explicitly authorize sub-agents, delegation, and parallel agent work".

### Entities

None — the plugin is stateless; all state lives in `core/coding_agent` tables.

## Data owned

No tables. No persistent state.

## UserOAuthApp registration

`bootstrap()` registers a `UserOAuthApp` (from `core/oauth`) with:

- `provider_id = "codex"`, `display_name = "Codex (ChatGPT)"`
- `flow = "device_code"` — RFC-8628 public client (no `client_secret`)
- `device_authorize_url = {YAAOS_CODEX_OAUTH_BASE_URL}/oauth/v2/device/code`
- `token_url = {YAAOS_CODEX_OAUTH_BASE_URL}/oauth/v2/token`
- `client_id = "openai-api-chatgpt"`, `default_scopes = ("openid", "profile", "email")`
- `expiry_source = "jwt_exp"`, `capture_id_token = True`
- `account_id_extractor` — reads `sub` (or `account_id`) from the id_token JWT payload

`YAAOS_CODEX_OAUTH_BASE_URL` defaults to `https://auth.openai.com`; override in test compose to point at the fake-openai peer.

`build_auth_json(credential)` serializes the `chatgptAuthTokens` shape required by the Codex CLI. The `refresh_token` field is always empty — the backend owns the refresh cycle via `ensure_fresh_access_token`.

## How it's tested

- `app/plugins/codex/test/test_parse_result_method.py` — `CodexPlugin.parse_result` unit.
- `app/plugins/codex/test/test_parse_activity_line.py` — `CodexPlugin.parse_activity_line` unit.
- `app/plugins/codex/test/test_validate_settings.py` — `CodexPlugin.validate_settings` unit.
- `app/plugins/codex/test/test_auth_json.py` — `build_auth_json` unit: `SecretStr` wrapping, exact `chatgptAuthTokens` shape, `None` id_token / account_id → empty strings.
- `app/core/coding_agent/test/test_skills_bundle.py` (codex renderer tests) — paths emit `.codex/` prefix; `AGENTS.md` contains delegation-authorization vocabulary; agent TOMLs include defensive restatement + correct TOML structure.
