"""core/plugin_kit — shared plugin-system primitives.

Today: `PluginMeta` + `PluginType`, the self-description every plugin
exposes via its `meta` attribute. Lives here (rather than in any specific
plugin or in a primitives grab-bag) so plugin discovery has a focused
entry point. Future plugin-system code (registry, validation, lifecycle
hooks) will land here too.
"""

from app.core.plugin_kit.service import PluginMeta, PluginType

__all__ = ["PluginMeta", "PluginType"]
