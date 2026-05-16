"""Foundational value objects + the spawn() helper.

`Actor` is the who-did-what value object used across audit_log, intake, reviewer, etc.
`PluginMeta` is the self-description every plugin (VCS, coding-agent, workspace
provider) exposes via its Protocol — id + type + display_name + optional
description/docs_url. Used by the Settings UI plugin-discovery endpoint and by
audit / log lines that reference plugins by something more legible than a code id.
`spawn()` is the fire-and-forget wrapper around asyncio.create_task — every background
coroutine in M01 goes through it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, model_validator

log = structlog.get_logger("primitives")


class ActorKind(StrEnum):
    GITHUB_USER = "github_user"
    AGENT = "agent"
    SYSTEM = "system"


class Actor(BaseModel):
    """Who-did-what. One value across the codebase.

    Invariants:
      - kind=github_user → login required, agent_id=None.
      - kind=agent → agent_id required, login=None.
      - kind=system → both None.
    """

    kind: ActorKind
    login: str | None = None
    agent_id: UUID | None = None

    @model_validator(mode="after")
    def _validate(self) -> Actor:
        if self.kind == ActorKind.GITHUB_USER:
            if not self.login:
                raise ValueError("Actor(github_user) requires login")
            if self.agent_id is not None:
                raise ValueError("Actor(github_user) must not have agent_id")
        elif self.kind == ActorKind.AGENT:
            if self.agent_id is None:
                raise ValueError("Actor(agent) requires agent_id")
            if self.login is not None:
                raise ValueError("Actor(agent) must not have login")
        else:  # system
            if self.login is not None or self.agent_id is not None:
                raise ValueError("Actor(system) must not have login or agent_id")
        return self

    @classmethod
    def system(cls) -> Actor:
        return cls(kind=ActorKind.SYSTEM)

    @classmethod
    def github_user(cls, login: str) -> Actor:
        return cls(kind=ActorKind.GITHUB_USER, login=login)

    @classmethod
    def agent(cls, agent_id: UUID) -> Actor:
        return cls(kind=ActorKind.AGENT, agent_id=agent_id)


PluginType = Literal["vcs", "coding_agent", "workspace"]


class PluginMeta(BaseModel):
    """Self-description every plugin exposes via `plugin.meta`.

    The `id` is the stable code identifier used everywhere a plugin is referenced
    by string (registry keys, URL paths under `/api/<id>/...`, agent rows'
    `coding_agent_plugin_id`, `Repo.plugin_id`, …). `display_name` is the human
    label; the UI shows that, not the id. `type` lets the UI group/format plugins
    by what they do.
    """

    id: str
    type: PluginType
    display_name: str
    description: str | None = None
    docs_url: str | None = None


# Module-level set keeps spawned tasks alive (asyncio's standard pitfall — without
# a strong reference, the GC may collect them mid-flight).
_tasks: set[asyncio.Task[Any]] = set()


def spawn(name: str, coro: Coroutine[Any, Any, None]) -> asyncio.Task[Any]:
    """Fire-and-forget background work.

    Wraps `coro` in a try/except that logs `spawn.crashed` with a stack trace
    if the coroutine raises. The coroutine itself is expected to mark its own
    domain row failed before raising; spawn() catches as a last-resort safety net.
    """

    async def _wrapper() -> None:
        try:
            await coro
        except Exception:
            logging.getLogger("yaaof").exception("spawn.crashed", extra={"spawn_name": name})

    task = asyncio.create_task(_wrapper(), name=f"spawn:{name}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    log.debug("spawn.started", spawn_name=name)
    return task


def active_task_count() -> int:
    """Test helper — number of pending spawned tasks."""
    return sum(1 for t in _tasks if not t.done())
