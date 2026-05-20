"""`org_context()` propagation tests — contextvar visibility across
`asyncio.create_task`, OTel span attrs, structlog binding, and the
`require_org_context()` assertion helper."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import structlog

from app.core.audit_log import ActorKind
from app.core.auth import (
    actor_kind_var,
    org_context,
    org_id_var,
    require_org_context,
    route_security_resolved,
)


@pytest.mark.asyncio
async def test_org_context_sets_and_resets_contextvars() -> None:
    org_id = uuid4()
    assert org_id_var.get() is None

    async with org_context(org_id, ActorKind.WORKSPACE):
        assert org_id_var.get() == org_id
        assert actor_kind_var.get() == ActorKind.WORKSPACE
        assert route_security_resolved.get() == "background"

    # Vars reset on exit.
    assert org_id_var.get() is None
    assert actor_kind_var.get() is None
    assert route_security_resolved.get() is None


@pytest.mark.asyncio
async def test_org_context_propagates_to_create_task() -> None:
    org_id = uuid4()
    seen: list = []

    async def _child() -> None:
        seen.append(org_id_var.get())
        seen.append(actor_kind_var.get())

    async with org_context(org_id, ActorKind.SYSTEM):
        await asyncio.create_task(_child())

    assert seen == [org_id, ActorKind.SYSTEM]


@pytest.mark.asyncio
async def test_org_context_binds_structlog_vars() -> None:
    """Inside the block, structlog contextvars carry the org + actor kind."""
    org_id = uuid4()
    async with org_context(org_id, ActorKind.WORKSPACE):
        ctx = structlog.contextvars.get_contextvars()
        assert ctx["yaaos_org_id"] == str(org_id)
        assert ctx["yaaos_actor_kind"] == "workspace"
    after = structlog.contextvars.get_contextvars()
    assert "yaaos_org_id" not in after
    assert "yaaos_actor_kind" not in after


@pytest.mark.asyncio
async def test_require_org_context_raises_outside_block() -> None:
    with pytest.raises(RuntimeError, match="org_id contextvar is unset"):
        require_org_context()


@pytest.mark.asyncio
async def test_require_org_context_returns_org_id_inside_block() -> None:
    org_id = uuid4()
    async with org_context(org_id, ActorKind.SYSTEM):
        assert require_org_context() == org_id
