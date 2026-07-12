# plugins/codex

> `CodingAgentPlugin` implementation wrapping the OpenAI Codex CLI (`@openai/codex`).

## Scope

Registers one `CodingAgentPlugin` with `plugin_id="codex"`, `command_kind="InvokeCodex"`. Owns skill-path convention (`.codex/skills/<skill_name>/SKILL.md`), Codex-specific argv/env construction, JSONL event parsing, OpenAI API key validation, static model/effort defaults, and the codex-native skills-bundle renderer.

Does NOT own: workspace mechanics, agent dispatch, run-lifecycle tables, or API key storage — those are `core/coding_agent` and `core/api_keys`.

## Public interface

- `CodexPlugin` — the `CodingAgentPlugin` implementation; registered at import time via `bootstrap()`.
- `build_auth_json(credential: UserOAuthCredential) -> SecretStr` — builds the `chatgptAuthTokens` JSON payload used by the Codex CLI's `CODEX_HOME/auth.json`. Returns a `SecretStr` so the plaintext never appears in logs.
- Bootstrap side effects: `register_plugin(CodexPlugin())` + `api_keys.register_validator("openai", validate_openai_key)` + `register_command_hydrator("InvokeCodex", _codex_command_hydrator)` + `register_user_oauth_app(UserOAuthApp(...))` + `register_credential_provider("codex", _codex_credential_provider)`. See § Credential provider and hydrator below.

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

## Credential provider and hydrator

Two functions registered at bootstrap handle per-user credential flows:

**`_codex_credential_provider(*, org_id, user_id, wallclock_seconds, session) -> CommandCredentialSpec`** — dispatch-time resolver called by `core/coding_agent.dispatch_invocation` for every `InvokeCodex` command. Reads the org's codex install `auth_mode` setting:

- `api_key` mode — checks `core/api_keys` for an org-level `"openai"` key; raises `CredentialUnavailableError("No OpenAI API key …")` if absent; returns `CommandCredentialSpec(credential_user_id=None)` if present (key arrives on the agent via `ConfigUpdate`).
- `per_user` mode — requires `user_id` (raises if `None`); calls `core/oauth.ensure_fresh_access_token(user_id, "codex", …)` to probe connection freshness without making HTTP calls when the token is valid (fast path: `access_token_expires_at > now + margin`); raises `CredentialUnavailableError` wrapping `ConnectionMissingError` ("not connected") or `ConnectionNeedsReauthError` ("re-authorization required"); on success returns `CommandCredentialSpec(credential_user_id=user_id)`. The dispatch-margin is `Settings.yaaos_codex_token_dispatch_margin_seconds` (default 3600 s).

**`_codex_command_hydrator(payload, session) -> dict`** — claim-time hydrator called by `core/agent_gateway` on every `InvokeCodex` command when it's claimed. Strips the gateway-internal `_org_id` key from the payload. For `per_user` mode (non-`None` `credential_user_id`): calls `ensure_fresh_access_token` again (the token may have expired in the window between dispatch and claim), calls `build_auth_json(credential)` to produce the `chatgptAuthTokens` JSON blob, and injects it as `auth_json: SecretStr` into the claimed payload. `credential_user_id` is kept in the output — the Go agent uses it as the signal to write `auth.json` from the injected payload. Raises `CredentialHydrationError` on `ConnectionMissingError` ("not connected") or `ConnectionNeedsReauthError` ("re-authorization required"); the gateway maps this to a `completed_failure` event with the error message as `failure_reason`. API-key mode passes through with no `auth_json` addition.

`auth_json` is always `SecretStr` — `.get_secret_value()` is called only in the Go agent's workspace child process, never in backend logs or error messages.

## How it's tested

- `app/plugins/codex/test/test_parse_result_method.py` — `CodexPlugin.parse_result` unit.
- `app/plugins/codex/test/test_parse_activity_line.py` — `CodexPlugin.parse_activity_line` unit.
- `app/plugins/codex/test/test_validate_settings.py` — `CodexPlugin.validate_settings` unit.
- `app/plugins/codex/test/test_auth_json.py` — `build_auth_json` unit: `SecretStr` wrapping, exact `chatgptAuthTokens` shape, `None` id_token / account_id → empty strings.
- `app/core/coding_agent/test/test_skills_bundle.py` (codex renderer tests) — paths emit `.codex/` prefix; `AGENTS.md` contains delegation-authorization vocabulary; agent TOMLs include defensive restatement + correct TOML structure.
- `app/plugins/codex/test/test_credential_provider_service.py` (`@pytest.mark.service`) — `_codex_credential_provider`: `api_key` mode with no key raises `CredentialUnavailableError`; with key returns `CommandCredentialSpec(credential_user_id=None)`; `per_user` mode with `user_id=None` raises; no connection raises; connected fresh token returns `CommandCredentialSpec(credential_user_id=user_id)`; `needs_reauth` connection raises.
- `app/plugins/codex/test/test_claim_hydrator_service.py` (`@pytest.mark.service`) — `_codex_command_hydrator`: `api_key` mode strips `_org_id` and adds no `auth_json`; `per_user` connected injects `auth_json` as `SecretStr` with correct `chatgptAuthTokens` shape and preserves `credential_user_id`; no connection raises `CredentialHydrationError`; `needs_reauth` raises `CredentialHydrationError`.
