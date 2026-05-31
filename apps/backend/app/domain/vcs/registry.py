"""Plugin registry for VCSPlugin instances."""

from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID

from app.core.plugin_kit import PluginMeta
from app.domain.vcs.types import PluginNotFoundError, VCSPlugin


class VCSRegistry:
    """VCS plugin map. ContextVar-bound so each test context gets a fresh,
    isolated instance; production rides the import-time default for the process
    lifetime — it never calls bind_vcs_registry(). The ContextVar exists solely
    for per-test isolation (see app/testing/isolation.py)."""

    def __init__(self) -> None:
        self._plugins: dict[str, VCSPlugin] = {}

    def register(self, plugin: VCSPlugin) -> None:
        if plugin.meta.id in self._plugins:
            raise ValueError(f"VCS plugin {plugin.meta.id!r} already registered")
        self._plugins[plugin.meta.id] = plugin

    def replace(self, plugin: VCSPlugin) -> None:
        """Overwrite-or-insert; used by stub helpers."""
        self._plugins[plugin.meta.id] = plugin

    def get(self, plugin_id: str) -> VCSPlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError as e:
            raise PluginNotFoundError(plugin_id) from e

    def is_registered(self, plugin_id: str) -> bool:
        return plugin_id in self._plugins

    def ids(self) -> list[str]:
        return list(self._plugins.keys())

    def metas(self) -> list[PluginMeta]:
        return [self._plugins[pid].meta for pid in sorted(self._plugins)]

    def copy(self) -> VCSRegistry:
        clone = VCSRegistry()
        clone._plugins = dict(self._plugins)
        return clone


_registry_var: ContextVar[VCSRegistry | None] = ContextVar("_vcs_registry_var", default=None)
# Import-time default: plugins that call register_vcs_plugin() at module-import
# time (bootstrap()) land here when no per-test binding is active. Production
# never calls bind_vcs_registry(); the ContextVar exists solely for per-test
# isolation.
_default_registry = VCSRegistry()


def bind_vcs_registry(instance: VCSRegistry) -> None:
    _registry_var.set(instance)


def current_vcs_registry() -> VCSRegistry:
    return _registry_var.get() or _default_registry


def register_vcs_plugin(plugin: VCSPlugin) -> None:
    current_vcs_registry().register(plugin)


def get_plugin(plugin_id: str) -> VCSPlugin:
    return current_vcs_registry().get(plugin_id)


def is_registered(plugin_id: str) -> bool:
    return current_vcs_registry().is_registered(plugin_id)


def registered_plugin_ids() -> list[str]:
    return current_vcs_registry().ids()


def list_plugin_metas() -> list[PluginMeta]:
    """Return `PluginMeta` for every registered VCS plugin, sorted by id."""
    return current_vcs_registry().metas()


async def get_installation_token(plugin_id: str, org_id: UUID) -> str:
    """Top-level dispatcher. Workspace plugins call this for fresh git auth."""
    plugin = get_plugin(plugin_id)
    return await plugin.get_installation_token(org_id)
