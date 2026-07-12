"""Minimal fake OpenAI Auth service for e2e tests.

Implements the RFC-8628 device-authorization endpoints used by the codex
OAuth user-connection flow:

  POST /device/code   — device-authorize
  POST /token         — token (returns pending until /__test/grant is called)

State is per device_code (in-memory). One /__test/ route:
  POST /__test/grant  — flip the in-flight pending code to granted
  POST /__test/reset  — clear all state (call at the start of each spec)
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import jwt as pyjwt  # PyJWT
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="fake-openai")

# In-memory state: {device_code: {"granted": bool, "user_code": str}}
_sessions: dict[str, dict[str, Any]] = {}

FAKE_ACCOUNT_ID = "chatgpt-fake-account-id"


def _make_jwt(subject: str) -> str:
    """Mint a minimal JWT with exp claim (1 hour)."""
    payload = {
        "sub": subject,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    return pyjwt.encode(payload, "fake-secret", algorithm="HS256")


# ── Device-auth endpoints ──────────────────────────────────────────────────────


@app.post("/oauth/v2/device/code")
@app.post("/device/code")
async def device_authorize() -> dict[str, Any]:
    device_code = f"dc_{uuid.uuid4().hex}"
    user_code = "TEST-1234"
    _sessions[device_code] = {"granted": False, "user_code": user_code}
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": "http://localhost/__test/activate",
        "expires_in": 900,
        "interval": 2,
    }


@app.post("/oauth/v2/token")
@app.post("/token")
async def token(
    grant_type: str = Form(...),
    device_code: str = Form(...),
    client_id: str = Form(...),
) -> JSONResponse:
    session = _sessions.get(device_code)
    if session is None:
        return JSONResponse(status_code=400, content={"error": "expired_token"})

    if not session["granted"]:
        return JSONResponse(status_code=400, content={"error": "authorization_pending"})

    # Grant issued.
    _sessions.pop(device_code, None)
    access_token = _make_jwt(FAKE_ACCOUNT_ID)
    id_token = _make_jwt(FAKE_ACCOUNT_ID)
    return JSONResponse(
        content={
            "access_token": access_token,
            "id_token": id_token,
            "expires_in": 3600,
            "scope": "openid",
        }
    )


# ���─ Test-control endpoints ─────────────────────────────────────────────────────


@app.post("/__test/grant")
async def test_grant() -> dict[str, Any]:
    """Flip all pending sessions to granted."""
    count = 0
    for session in _sessions.values():
        if not session["granted"]:
            session["granted"] = True
            count += 1
    return {"granted": count}


@app.post("/__test/reset")
async def test_reset() -> dict[str, Any]:
    _sessions.clear()
    return {"ok": True}


@app.get("/__test/status")
async def test_status() -> dict[str, Any]:
    return {"pending": sum(1 for s in _sessions.values() if not s["granted"])}
