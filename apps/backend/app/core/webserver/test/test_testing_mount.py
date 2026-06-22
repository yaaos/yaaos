"""Unit tests for core/webserver testing_mount helpers.

Verifies:
- mount_testing_endpoints raises RuntimeError when called with is_production=True.
- assert_no_testing_routes_in_prod raises RuntimeError when /api/testing/* routes
  are present on the app in production mode.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from fastapi import FastAPI

from app.core.webserver.testing_mount import (
    assert_no_testing_routes_in_prod,
    mount_testing_endpoints,
)


@dataclasses.dataclass
class _StubSettings:
    """Minimal settings stub for the mount-helper unit tests."""

    is_production: bool


def test_prod_mount_refuses_testing_endpoints() -> None:
    """mount_testing_endpoints raises RuntimeError when is_production=True."""
    settings = _StubSettings(is_production=True)
    with pytest.raises(RuntimeError, match="cannot be called in production"):
        mount_testing_endpoints(FastAPI(), settings)  # type: ignore[arg-type]


def test_assert_no_testing_routes_in_prod_fires() -> None:
    """assert_no_testing_routes_in_prod raises when /api/testing/* routes exist in prod."""
    app = FastAPI()

    @app.get("/api/testing/foo")
    async def _foo() -> dict[str, Any]:
        return {}

    settings = _StubSettings(is_production=True)
    with pytest.raises(RuntimeError, match="/api/testing/foo"):
        assert_no_testing_routes_in_prod(app, settings)  # type: ignore[arg-type]


def test_assert_no_testing_routes_in_prod_noop_in_non_prod() -> None:
    """assert_no_testing_routes_in_prod is a no-op when is_production=False."""
    app = FastAPI()

    @app.get("/api/testing/foo")
    async def _foo() -> dict[str, Any]:
        return {}

    settings = _StubSettings(is_production=False)
    # Should not raise even though a testing route exists.
    assert_no_testing_routes_in_prod(app, settings)  # type: ignore[arg-type]
