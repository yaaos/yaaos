"""Contextvars + helpers for propagating identity through the request and
through background work.

HTTP middleware sets these per request (see `middleware.py`). Background jobs
open `org_context(...)` to set the same vars + OTel span attrs + structlog
contextvars (filled in by Phase 9).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Literal
from uuid import UUID

import structlog

from app.core.primitives import ActorKind

log = structlog.get_logger("auth.context")


# Identity contextvars. None when no value resolved (anonymous request,
# background work before `org_context()` entered, etc.).
org_id_var: ContextVar[UUID | None] = ContextVar("yaaos_org_id", default=None)
user_id_var: ContextVar[UUID | None] = ContextVar("yaaos_user_id", default=None)
actor_kind_var: ContextVar[ActorKind | None] = ContextVar("yaaos_actor_kind", default=None)
actor_id_var: ContextVar[UUID | None] = ContextVar("yaaos_actor_id", default=None)

# Set by `require(action)` / `public_route` deps. The middleware's post-response
# guard checks this; an unset value on a protected route signals a missing
# security declaration and returns 500.
SecurityResolution = Literal["membership", "public", "background"]
route_security_resolved: ContextVar[SecurityResolution | None] = ContextVar(
    "yaaos_route_security_resolved", default=None
)


def clear_request_context() -> None:
    """Reset every var to None. Called by middleware at request start."""
    org_id_var.set(None)
    user_id_var.set(None)
    actor_kind_var.set(None)
    actor_id_var.set(None)
    route_security_resolved.set(None)


def current_org_id() -> UUID | None:
    return org_id_var.get()


def current_user_id() -> UUID | None:
    return user_id_var.get()


def current_actor_kind() -> ActorKind | None:
    return actor_kind_var.get()


@asynccontextmanager
async def org_context(
    org_id: UUID,
    actor_kind: ActorKind,
    actor_id: UUID | None = None,
):
    """Background-job entry point — sets the same contextvars HTTP middleware
    sets. Phase 9 extends this with OTel span attrs + structlog vars; the
    minimum implementation lands here so Phase 2 tests for `current_*`
    helpers have a non-HTTP code path.
    """
    org_token = org_id_var.set(org_id)
    kind_token = actor_kind_var.set(actor_kind)
    actor_token = actor_id_var.set(actor_id)
    sec_token = route_security_resolved.set("background")
    try:
        yield
    finally:
        route_security_resolved.reset(sec_token)
        actor_id_var.reset(actor_token)
        actor_kind_var.reset(kind_token)
        org_id_var.reset(org_token)
