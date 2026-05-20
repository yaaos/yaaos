"""PluginMeta + PluginType — self-description every plugin exposes via `plugin.meta`.

Single-file module: two tiny classes that need a stable home outside any
specific plugin's `__init__.py`. Previously in `core/primitives`; relocated
here in M04 Phase 6a so plugin discovery has a focused entry point.
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
