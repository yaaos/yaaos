"""Intake-type registry.

A registered `IntakeType` is the entry point for an inbound signal: it
verifies authenticity, parses the payload, and applies its mutations directly
inside the endpoint's session, returning `IntakeSideEffect` — for events that
adjust existing state (PR open/close/reopen, install lifecycle, comment threads).

Registry is process-local. Types register themselves at import time from
within `domain/intake` or via plugin bootstrap.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable
from uuid import UUID  # noqa: F401 — kept for downstream imports that re-export

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


class IntakeSideEffect(BaseModel):
    """Non-ticket outcome from `IntakeType.handle()`. The handler already
    applied its mutations against the endpoint's session; the endpoint just
    commits and returns 200. `detail` is included in the response body for
    observability."""

    model_config = ConfigDict(frozen=True)
    detail: str = "side_effect"


# All handlers return IntakeSideEffect — each manages its own ticket creation.
IntakeOutcome = IntakeSideEffect


@runtime_checkable
class IntakeType(Protocol):
    """Per-type intake handler. Implementations register themselves with the
    process-wide registry via `register_intake_type`."""

    name: str

    async def handle(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        session: AsyncSession,
    ) -> IntakeOutcome: ...


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
