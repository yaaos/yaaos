"""Intake-type registry.

A registered `IntakeType` is the M05 entry point for an inbound signal: it
verifies authenticity, parses the payload, and produces an `IntakePrepared`
that the `/api/intake/{type}` endpoint turns into a ticket + workflow start.

Each type maps to exactly one workflow (`workflow_name`). M05 ships the
`github_pr` type bound to `pr_review_v1`.

Registry is process-local. Types register themselves at import time from
within `domain/intake` (or via plugin bootstrap if we ever route external
signals through plugins).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class IntakeRejectedError(Exception):
    """Raised by an `IntakeType.handle()` when the request is rejected
    before ticket creation. The endpoint maps each kind to an HTTP status:

    - `bad_signature` → 401
    - `bad_request` → 400
    - `unsupported` → 422
    """

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind


class IntakePrepared(BaseModel):
    """The output of `IntakeType.handle()`. Drives `domain/tickets.create()`
    and `core/workflow.start()`."""

    model_config = ConfigDict(frozen=True)
    org_id: UUID
    idempotency_key: str
    title: str | None = None
    description: str | None = None
    payload: Mapping[str, Any]
    source_external_id: str | None = None
    repo_external_id: str = ""


@runtime_checkable
class IntakeType(Protocol):
    """Per-type intake handler. Implementations register themselves with the
    process-wide registry via `register_intake_type`."""

    name: str
    workflow_name: str

    async def handle(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        session: AsyncSession,
    ) -> IntakePrepared: ...


_REGISTRY: dict[str, IntakeType] = {}


def register_intake_type(intake_type: IntakeType) -> None:
    """Register an `IntakeType`. Re-registering the same `name` raises —
    use `_reset_registry_for_tests()` between tests that need a clean slate."""
    if intake_type.name in _REGISTRY:
        raise ValueError(f"intake type '{intake_type.name}' already registered")
    _REGISTRY[intake_type.name] = intake_type


def get_intake_type(name: str) -> IntakeType | None:
    return _REGISTRY.get(name)


def registered_intake_types() -> list[str]:
    return sorted(_REGISTRY.keys())


def _reset_registry_for_tests() -> None:
    _REGISTRY.clear()
