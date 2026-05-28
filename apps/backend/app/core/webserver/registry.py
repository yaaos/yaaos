"""`RouteSpec` model + `register_routes` registry with one-prefix-per-module enforcement.

See  for the full contract.
"""

from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field


class RouteSpec(BaseModel):
    """A module's HTTP registration. Submitted via `register_routes()` at import time."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    module_name: str = Field(..., min_length=1, description="OpenAPI tag + telemetry key.")
    url_prefix: str | None = Field(
        default=None,
        description="Optional override; defaults to f'/api/{module_name}'.",
    )
    router: APIRouter = Field(..., description="MUST NOT carry its own prefix.")
    on_startup: list[Callable[[], Awaitable[None]]] = Field(default_factory=list)
    on_shutdown: list[Callable[[], Awaitable[None]]] = Field(default_factory=list)

    @property
    def effective_prefix(self) -> str:
        """The URL prefix this RouteSpec will mount at — explicit override or
        `/api/{module_name}` fallback. The single source of truth used by both
        the production app factory and any test that mounts specs."""
        return self.url_prefix or f"/api/{self.module_name}"


# Keyed by module_name for O(1) uniqueness; insertion order is preserved (Python 3.7+).
_specs: dict[str, RouteSpec] = {}
# effective_prefix -> module_name, for overlap detection.
_claimed_prefixes: dict[str, str] = {}


def register_routes(spec: RouteSpec) -> None:
    """Validate and append a RouteSpec. Raises ValueError on any violation.

    Rules (enforced at import time so failures surface in the offending module's traceback):
      1. The passed router must NOT carry its own prefix.
      2. `module_name` is unique across all registrations.
      3. The effective prefix is unique AND non-overlapping with any other.
      4. The effective prefix starts with `/api/` and does not end with `/`.
    """
    if spec.router.prefix:
        raise ValueError(
            f"{spec.module_name}: router must not carry its own prefix "
            f"(got {spec.router.prefix!r}); set url_prefix on the RouteSpec instead."
        )
    if spec.module_name in _specs:
        raise ValueError(f"module {spec.module_name!r} already registered routes")
    prefix = spec.effective_prefix
    if not prefix.startswith("/api/") or prefix.endswith("/"):
        raise ValueError(
            f"{spec.module_name}: url_prefix must start with '/api/' and not end with '/' (got {prefix!r})"
        )
    for claimed, claimant in _claimed_prefixes.items():
        if prefix == claimed or prefix.startswith(claimed + "/") or claimed.startswith(prefix + "/"):
            raise ValueError(
                f"{spec.module_name}: prefix {prefix!r} overlaps with {claimed!r} (owned by {claimant!r})"
            )
    _specs[spec.module_name] = spec
    _claimed_prefixes[prefix] = spec.module_name


def get_specs() -> dict[str, RouteSpec]:
    """Internal — used by the app factory to mount routers + run lifecycle hooks."""
    return _specs
