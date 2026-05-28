"""Plugin registry for VCSPlugin instances."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from app.core.plugin_kit import PluginMeta
from app.domain.vcs.types import PluginNotFoundError, VCSPlugin

_PLUGINS: dict[str, VCSPlugin] = {}


def register_vcs_plugin(plugin: VCSPlugin) -> None:
    if plugin.meta.id in _PLUGINS:
        raise ValueError(f"VCS plugin {plugin.meta.id!r} already registered")
    _PLUGINS[plugin.meta.id] = plugin


def unregister_vcs_plugin(plugin_id: str) -> None:
    """Remove a VCS plugin from the registry. No-op if not registered."""
    _PLUGINS.pop(plugin_id, None)


@contextmanager
def scoped_vcs_plugin(plugin: VCSPlugin) -> Iterator[VCSPlugin]:
    """Context manager: install *plugin* for the duration of the block, then
    restore the prior entry (if any) on exit — even if an exception is raised.

    If *plugin.meta.id* is already registered, the prior entry is saved and
    replaced; on exit the prior entry is restored. If the id was not registered,
    the plugin is simply unregistered on exit."""
    prior = _PLUGINS.get(plugin.meta.id)
    _PLUGINS[plugin.meta.id] = plugin
    try:
        yield plugin
    finally:
        if prior is None:
            _PLUGINS.pop(plugin.meta.id, None)
        else:
            _PLUGINS[plugin.meta.id] = prior


def get_plugin(plugin_id: str) -> VCSPlugin:
    try:
        return _PLUGINS[plugin_id]
    except KeyError as e:
        raise PluginNotFoundError(plugin_id) from e


def is_registered(plugin_id: str) -> bool:
    return plugin_id in _PLUGINS


def registered_plugin_ids() -> list[str]:
    return list(_PLUGINS.keys())


def list_plugin_metas() -> list[PluginMeta]:
    """Return `PluginMeta` for every registered VCS plugin, sorted by id."""
    return [_PLUGINS[pid].meta for pid in sorted(_PLUGINS)]


async def get_installation_token(plugin_id: str, org_id: UUID) -> str:
    """Top-level dispatcher. Workspace plugins call this for fresh git auth."""
    plugin = get_plugin(plugin_id)
    return await plugin.get_installation_token(org_id)
