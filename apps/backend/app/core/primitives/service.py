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
    # M02 — identity & access. A real yaaos user (uuid PK), a workspace
    # principal (background jobs running on behalf of an org), an SSO
    # assertion (used when an audit row's only "who" is a verified IdP).
    USER = "user"
    WORKSPACE = "workspace"
    SSO = "sso"


class Actor(BaseModel):
    """Who-did-what. One value across the codebase.

    Invariants:
      - kind=github_user → login required; user_id/agent_id/workspace_id None.
      - kind=agent → agent_id required; login/user_id/workspace_id None.
      - kind=system → all id fields None.
      - kind=user → user_id required; login optional (display login);
        agent_id/workspace_id None.
      - kind=workspace → workspace_id required; everything else None.
      - kind=sso → all id fields None (only the IdP knew); login optional.
    """

    kind: ActorKind
    login: str | None = None
    agent_id: UUID | None = None
    user_id: UUID | None = None
    workspace_id: UUID | None = None

    @model_validator(mode="after")
    def _validate(self) -> Actor:
        if self.kind == ActorKind.GITHUB_USER:
            if not self.login:
                raise ValueError("Actor(github_user) requires login")
            if self.agent_id is not None or self.user_id is not None or self.workspace_id is not None:
                raise ValueError("Actor(github_user) must carry only login")
        elif self.kind == ActorKind.AGENT:
            if self.agent_id is None:
                raise ValueError("Actor(agent) requires agent_id")
            if self.login is not None or self.user_id is not None or self.workspace_id is not None:
                raise ValueError("Actor(agent) must carry only agent_id")
        elif self.kind == ActorKind.SYSTEM:
            if any((self.login, self.agent_id, self.user_id, self.workspace_id)):
                raise ValueError("Actor(system) must not carry any id")
        elif self.kind == ActorKind.USER:
            if self.user_id is None:
                raise ValueError("Actor(user) requires user_id")
            if self.agent_id is not None or self.workspace_id is not None:
                raise ValueError("Actor(user) must not carry agent_id or workspace_id")
        elif self.kind == ActorKind.WORKSPACE:
            if self.workspace_id is None:
                raise ValueError("Actor(workspace) requires workspace_id")
            if self.login is not None or self.agent_id is not None or self.user_id is not None:
                raise ValueError("Actor(workspace) must carry only workspace_id")
        else:  # sso
            if self.agent_id is not None or self.user_id is not None or self.workspace_id is not None:
                raise ValueError("Actor(sso) carries no domain ids — IdP-only")
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

    @classmethod
    def user(cls, user_id: UUID, login: str | None = None) -> Actor:
        return cls(kind=ActorKind.USER, user_id=user_id, login=login)

    @classmethod
    def workspace(cls, workspace_id: UUID) -> Actor:
        return cls(kind=ActorKind.WORKSPACE, workspace_id=workspace_id)

    @classmethod
    def sso(cls, login: str | None = None) -> Actor:
        return cls(kind=ActorKind.SSO, login=login)


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
            logging.getLogger("yaaos").exception("spawn.crashed", extra={"spawn_name": name})

    task = asyncio.create_task(_wrapper(), name=f"spawn:{name}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    log.debug("spawn.started", spawn_name=name)
    return task


def active_task_count() -> int:
    """Test helper — number of pending spawned tasks."""
    return sum(1 for t in _tasks if not t.done())
