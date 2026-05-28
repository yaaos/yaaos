"""Contextvars + helpers for propagating identity through the request and
through background work.

HTTP middleware sets these per request (see `middleware.py`). Background jobs
open `org_context(...)` to set the same vars + OTel span attrs + structlog
contextvars.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Literal
from uuid import UUID

import structlog

from app.core.audit_log import ActorKind

log = structlog.get_logger("auth.context")


# Identity contextvars. None when no value resolved (anonymous request,
# background work before `org_context()` entered, etc.).
org_id_var: ContextVar[UUID | None] = ContextVar("yaaos_org_id", default=None)
user_id_var: ContextVar[UUID | None] = ContextVar("yaaos_user_id", default=None)
actor_kind_var: ContextVar[ActorKind | None] = ContextVar("yaaos_actor_kind", default=None)
actor_id_var: ContextVar[UUID | None] = ContextVar("yaaos_actor_id", default=None)

# Set by the middleware (for PUBLIC / USER_SCOPED paths) and by route deps
# (`require(action)` sets "org_scoped" once it resolves a membership;
# `public_route` sets "public" as a marker for explicitly-public routes).
# The post-response guard rejects any /api/* 2xx response that left this
# unset — that means a route lacks an auth declaration.
SecurityResolution = Literal["public", "user_scoped", "org_scoped", "background"]
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
    sets, plus OTel span attrs + structlog bound contextvars."""
    from opentelemetry import trace  # noqa: PLC0415

    org_token = org_id_var.set(org_id)
    kind_token = actor_kind_var.set(actor_kind)
    actor_token = actor_id_var.set(actor_id)
    sec_token = route_security_resolved.set("background")

    span = trace.get_current_span()
    if span is not None:
        span.set_attribute("yaaos.org_id", str(org_id))
        span.set_attribute("yaaos.actor_kind", actor_kind.value)
        if actor_id is not None:
            span.set_attribute("yaaos.actor_id", str(actor_id))

    structlog.contextvars.bind_contextvars(
        yaaos_org_id=str(org_id),
        yaaos_actor_kind=actor_kind.value,
        yaaos_actor_id=str(actor_id) if actor_id is not None else None,
    )

    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars("yaaos_org_id", "yaaos_actor_kind", "yaaos_actor_id")
        route_security_resolved.reset(sec_token)
        actor_id_var.reset(actor_token)
        actor_kind_var.reset(kind_token)
        org_id_var.reset(org_token)


_REQUEST_STRUCTLOG_KEYS = ("yaaos_org_id", "yaaos_user_id", "yaaos_actor_kind", "yaaos_actor_id")


def bind_request_structlog_vars() -> None:
    """Bind the currently-resolved identity contextvars to structlog so every
    log line inside the request carries them. Idempotent — called after
    `require()` resolves a session + membership."""
    org_id = org_id_var.get()
    user_id = user_id_var.get()
    actor_kind = actor_kind_var.get()
    actor_id = actor_id_var.get()
    bindings: dict[str, str] = {}
    if org_id is not None:
        bindings["yaaos_org_id"] = str(org_id)
    if user_id is not None:
        bindings["yaaos_user_id"] = str(user_id)
    if actor_kind is not None:
        bindings["yaaos_actor_kind"] = actor_kind.value
    if actor_id is not None:
        bindings["yaaos_actor_id"] = str(actor_id)
    if bindings:
        structlog.contextvars.bind_contextvars(**bindings)


def unbind_request_structlog_vars() -> None:
    """Clear structlog bindings at request end. Safe to call even when no
    binding ever happened — `unbind_contextvars` no-ops on missing keys."""
    structlog.contextvars.unbind_contextvars(*_REQUEST_STRUCTLOG_KEYS)


async def public_route() -> None:
    """FastAPI dependency marker for `RouteSecurity.PUBLIC` routes. Sets
    `route_security_resolved = "public"` so the middleware's post-response
    guard recognizes the declaration. Lives in `core/auth` (not
    `core/sessions`) so modules that don't need session resolution can use it
    without loading the full sessions module. USER_SCOPED routes don't need this marker — the middleware sets
    `"user_scoped"` based on path classification."""
    route_security_resolved.set("public")


def require_org_context() -> UUID:
    """Assertion helper for functions that read org-scoped state. Raises
    `RuntimeError` outside an HTTP middleware or `org_context()` block —
    surfaces forgotten-context bugs loudly instead of silently leaking
    cross-org data."""
    org_id = org_id_var.get()
    if org_id is None:
        raise RuntimeError(
            "org_id contextvar is unset — wrap this code in `org_context(...)` "
            "or pass `org_id` as an explicit parameter"
        )
    return org_id
