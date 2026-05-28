# core/plugin_kit

> Shared primitives every plugin uses to describe itself.

## Scope

- Owns: `PluginMeta`, `PluginType`.
- Does NOT own: per-type registries — those live in `domain/vcs`, `domain/coding_agent`, `core/workspace`.

## Why / invariants

Two Pydantic types, no state, no registry. Lives at `core/plugin_kit` (not inside any specific plugin) so plugin discovery has a focused entry point.

