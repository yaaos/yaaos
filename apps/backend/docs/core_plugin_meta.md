# core/plugin_meta

> Self-description every plugin exposes via `plugin.meta`.

## Purpose

`PluginMeta` is the value object the Settings UI plugin-discovery endpoint reads to render the picker, and that audit + log lines reference for human-legible plugin names. Single-file module — two tiny Pydantic types whose home is "next to plugin discovery" rather than buried in a primitives grab-bag.

## Public interface

- `PluginType` — `Literal["vcs", "coding_agent", "workspace"]`. The dimension the UI groups plugins by.
- `PluginMeta` — `id`, `type`, `display_name`, optional `description` and `docs_url`. Each plugin exposes one of these via its `meta` attribute.

## Module architecture

Single file. No state, no registry — the per-type registries live in the consuming modules (`domain/vcs`, `domain/coding_agent`, `core/workspace`).

## Data owned

None.

## How it's tested

Type assertions in the plugin-registry tests of each consumer (`app.domain.vcs.registry`, `app.domain.coding_agent.service`, `app.core.workspace.service`).
