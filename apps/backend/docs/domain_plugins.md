# domain/plugins

> Cross-registry plugin enumeration for the settings picker UI.

## Purpose

Single dispatcher that, given a `PluginType` (`vcs` or `coding_agent`), returns every registered plugin's `PluginMeta`. The settings UI consumes this via `GET /api/plugins/available?type=...` so the picker is data-driven — no plugin id is hardcoded in the frontend.

## Public interface

- `list_available(plugin_type) -> list[PluginMeta]` — delegates to `domain/vcs.list_plugin_metas()` or `domain/coding_agent.list_plugin_metas()`. Returns `[]` for `workspace` (workspace plugins are infra, never picker-visible).
- `GET /api/plugins/available?type={vcs|coding_agent}` — gated on `Action.MEMBERS_READ`. Returns `{plugins: [{id, type, display_name, description, docs_url}, ...]}`.

## Module architecture

Pure dispatcher — no storage, no business logic. The two underlying registries (`domain/vcs._PLUGINS`, `domain/coding_agent._PLUGINS`) own plugin instances; this module reads their `.meta`. Adding a new picker-visible plugin type means adding a branch here and a new registry; M03 ships two.

## Data owned

None.

## How it's tested

- `test_list_available.py` — service-level lookups + the endpoint's auth (401 unauthenticated, type filter actually filters, 422 on invalid type).
- `test_plugin_contract.py` — the three shipped plugins (`github`, `claude_code`, `in_process`) expose the M03 contract methods (`install_url`, `validate_settings`).
