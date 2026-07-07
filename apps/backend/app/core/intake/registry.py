"""Intake-type + intake-point registry.

A registered `IntakeType` is the entry point for an inbound signal: it
verifies authenticity, parses the payload, and applies its mutations directly
inside the endpoint's session, returning `IntakeSideEffect` — for events that
adjust existing state (PR open/close/reopen, install lifecycle, comment threads).

A registered `IntakePoint` is a plugin-contributed trigger source (e.g.
`github:pr_opened`) that `domain/repos` trigger bindings target — the
Repos-page picker lists them via `list_intake_points()`.

Registry is process-local, ContextVar-bound (standard pattern — see
`apps/backend/docs/patterns.md § Cardinal rule`): production rides the
import-time default for the process lifetime; `set_intake_for_tests` is the
sole test-isolation seam, binding a fresh copy per test.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, Protocol, runtime_checkable
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


class IntakePoint(BaseModel, frozen=True):
    """Plugin-contributed trigger metadata the Repos-page trigger picker
    lists. `id` is the value `domain/repos` trigger bindings target (e.g.
    `"github:pr_opened"`, `"schedule"`)."""

    id: str
    plugin_id: str | None
    label: str
    kind: Literal["webhook", "schedule"]


class IntakeRegistry:
    """Holds both `IntakeType`s (keyed by `name`) and `IntakePoint`s (keyed
    by `id`) — the two independent contribution surfaces `core/intake`
    exposes to plugins."""

    def __init__(self) -> None:
        self._types: dict[str, IntakeType] = {}
        self._points: dict[str, IntakePoint] = {}

    def register_type(self, intake_type: IntakeType) -> None:
        if intake_type.name in self._types:
            raise ValueError(f"intake type '{intake_type.name}' already registered")
        self._types[intake_type.name] = intake_type

    def get_type(self, name: str) -> IntakeType | None:
        return self._types.get(name)

    def type_names(self) -> list[str]:
        return sorted(self._types)

    def register_point(self, point: IntakePoint) -> None:
        if point.id in self._points:
            raise ValueError(f"intake point '{point.id}' already registered")
        self._points[point.id] = point

    def points(self) -> tuple[IntakePoint, ...]:
        return tuple(sorted(self._points.values(), key=lambda p: p.id))

    def copy(self) -> IntakeRegistry:
        clone = IntakeRegistry()
        clone._types = dict(self._types)
        clone._points = dict(self._points)
        return clone


_registry_var: ContextVar[IntakeRegistry | None] = ContextVar("_intake_registry_var", default=None)


def _get() -> IntakeRegistry:
    val = _registry_var.get()
    if val is None:
        val = IntakeRegistry()
        _registry_var.set(val)
    return val


def register_intake_type(intake_type: IntakeType) -> None:
    """Register an `IntakeType`. Re-registering the same `name` raises —
    use `set_intake_for_tests()` between tests that need a clean slate."""
    _get().register_type(intake_type)


def get_intake_type(name: str) -> IntakeType | None:
    return _get().get_type(name)


def registered_intake_types() -> list[str]:
    return _get().type_names()


def register_intake_point(point: IntakePoint) -> None:
    """Register an `IntakePoint`. Re-registering the same `id` raises."""
    _get().register_point(point)


def list_intake_points() -> tuple[IntakePoint, ...]:
    """Every registered intake point, sorted by `id` — feeds the Repos-page
    trigger picker (`GET /api/intake/points`) and `domain/repos.add_binding`'s
    `intake_point_id` validation."""
    return _get().points()


@contextmanager
def set_intake_for_tests(*, scenario: Literal["default", "empty"] = "default") -> Iterator[IntakeRegistry]:
    """Bind an isolated registry for the duration of the `with` block.

    ``scenario="default"`` gives a copy of the current registry (types +
    points registered so far — e.g. `plugins/github`'s three intake points
    once that package has been imported); ``scenario="empty"`` gives a
    brand-new empty registry. Restores the prior binding on exit — even on
    exception.
    """
    instance = IntakeRegistry() if scenario == "empty" else _get().copy()
    token = _registry_var.set(instance)
    try:
        yield instance
    finally:
        _registry_var.reset(token)
