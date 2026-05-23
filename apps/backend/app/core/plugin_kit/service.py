"""PluginMeta + PluginType — self-description every plugin exposes via `plugin.meta`.

Lives in `core/plugin_kit` so plugin discovery has a focused home outside
any specific plugin. Today this is just the two metadata types; future
plugin-system primitives (registry, validation, discovery) land here too.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

PluginType = Literal["vcs", "coding_agent", "workspace"]


class PluginMeta(BaseModel):
    """Self-description every plugin exposes via `plugin.meta`.

    The `id` is the stable code identifier used everywhere a plugin is
    referenced by string (registry keys, URL paths under `/api/<id>/...`,
    agent rows' `coding_agent_plugin_id`, `Repo.plugin_id`, …). `display_name`
    is the human label; the UI shows that, not the id. `type` lets the UI
    group/format plugins by what they do.
    """

    id: str
    type: PluginType
    display_name: str
    description: str | None = None
    docs_url: str | None = None


__all__ = ["PluginMeta", "PluginType"]
