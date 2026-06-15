"""POST /api/intake/{type} — happy path (side_effect), idempotent duplicate, rejection codes."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.intake import (
    IntakeRejectedError,
    IntakeSideEffect,
    register_intake_type,
)
from app.core.intake import web as _intake_web  # noqa: F401 — registers routes
from app.core.intake.registry import _reset_registry_for_tests


class _StubIntakeType:
    """Intake type used in tests — no GitHub. Header `X-Yaaos-Stub-Auth: ok` is
    required (a missing/wrong header maps to `bad_signature` → 401).

    Returns `IntakeSideEffect` matching the production convention — every
    handler owns its own ticket creation inside the session."""

    name = "stub_pr"

    async def handle(self, *, headers, body, session) -> IntakeSideEffect:
        if headers.get("x-yaaos-stub-auth") != "ok":
            raise IntakeRejectedError("bad_signature")
        detail = headers.get("x-yaaos-stub-detail", "stub_done")
        return IntakeSideEffect(detail=detail)


@pytest_asyncio.fixture
async def stub_intake(db_session):  # type: ignore[no-untyped-def]
    """Register the stub intake type for the duration of the test."""
    _reset_registry_for_tests()
    register_intake_type(_StubIntakeType())
    yield {}
    _reset_registry_for_tests()
    import importlib  # noqa: PLC0415

    import app.core.intake as intake_mod  # noqa: PLC0415

    importlib.reload(intake_mod)


def _app() -> FastAPI:

    app = FastAPI()
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"intake"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest.mark.asyncio
async def test_unknown_intake_type_404(db_session, stub_intake) -> None:
    async with _client() as c:
        r = await c.post("/api/intake/ghost", content=b"{}")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "unknown_intake_type"


@pytest.mark.asyncio
async def test_bad_signature_returns_401(db_session, stub_intake) -> None:
    async with _client() as c:
        r = await c.post("/api/intake/stub_pr", content=b"{}", headers={})
    assert r.status_code == 401
    assert r.json()["error"] == "bad_signature"


@pytest.mark.asyncio
async def test_happy_path_returns_side_effect(db_session, stub_intake) -> None:
    async with _client() as c:
        r = await c.post(
            "/api/intake/stub_pr",
            content=b"{}",
            headers={"X-Yaaos-Stub-Auth": "ok", "X-Yaaos-Stub-Detail": "pr_review_started"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "side_effect"
    assert body["detail"] == "pr_review_started"


@pytest.mark.asyncio
async def test_default_detail_returned_when_not_specified(db_session, stub_intake) -> None:
    async with _client() as c:
        r = await c.post(
            "/api/intake/stub_pr",
            content=b"{}",
            headers={"X-Yaaos-Stub-Auth": "ok"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["detail"] == "stub_done"
