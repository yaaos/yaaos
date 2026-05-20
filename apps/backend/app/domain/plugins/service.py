"""Cross-registry plugin enumeration.

Dispatches to the appropriate registry's `list_plugin_metas()` based on
`PluginType`. Powers the picker UI (`GET /api/plugins/available?type=...`).
"""

from __future__ import annotations

from app.core.plugin_meta import PluginMeta, PluginType
from app.domain.coding_agent import list_plugin_metas as _coding_agent_metas
from app.domain.vcs import list_plugin_metas as _vcs_metas


def list_available(plugin_type: PluginType) -> list[PluginMeta]:
    """Return every registered plugin's `PluginMeta` for the requested type."""
    if plugin_type == "vcs":
        return _vcs_metas()
    if plugin_type == "coding_agent":
        return _coding_agent_metas()
    # `workspace` plugins exist but are infra — never user-pickable.
    return []
