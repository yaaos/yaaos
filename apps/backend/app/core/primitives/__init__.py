"""core/primitives — Actor + PluginMeta + spawn helper. Bottom of the dependency tree."""

from app.core.primitives.service import (
    Actor,
    ActorKind,
    PluginMeta,
    PluginType,
    active_task_count,
    spawn,
)

__all__ = ["Actor", "ActorKind", "PluginMeta", "PluginType", "active_task_count", "spawn"]
