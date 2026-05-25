# core/plugin_kit

> Shared primitives every plugin uses to describe itself.

## Purpose

Houses `PluginMeta` + `PluginType` — the self-description every plugin exposes via `plugin.meta`. Lives at `core/plugin_kit` (rather than buried in a specific plugin or in a primitives grab-bag) so plugin discovery has a focused entry point.

## Public interface

- `PluginType` — `Literal["vcs", "coding_agent", "workspace"]`. The dimension the UI groups plugins by.
- `PluginMeta` — `id`, `type`, `display_name`, optional `description` and `docs_url`. Each plugin exposes one via its `meta` attribute.

## Module architecture

Two Pydantic types, no state, no registry. Per-type registries live in the consuming modules (`domain/vcs`, `domain/coding_agent`, `core/workspace`).

## Data owned

None.

## How it's tested

Type assertions in the plugin-registry tests of each consumer (`app.domain.vcs.registry`, `app.domain.coding_agent.service`, `app.core.workspace.service`).
