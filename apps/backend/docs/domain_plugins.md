# domain/plugins

> Cross-registry plugin enumeration for the settings picker UI.

## Scope

Pure dispatcher — given a `PluginType` (`vcs` or `coding_agent`), returns every registered plugin's `PluginMeta`. No storage, no business logic.

## Data owned

None.

## How it's tested

- `test/test_list_available.py` — service lookups + endpoint auth (401, type filter, 422 on invalid type).
- `test/test_plugin_contract.py` — shipped plugins (`github`, `claude_code`) expose `install_url` and `validate_settings`.
