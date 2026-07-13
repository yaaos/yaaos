"""Service tests for the codex claim-time credential hydrator.

Covers `_codex_command_hydrator`'s sole behavior: api_key mode is a
passthrough — the Go agent reads `CODEX_API_KEY` from the ConfigUpdate
`api_keys` map, so no credential injection happens at claim time.

Direct-invocation style (no claim_next machinery) — the hydrator is tested as a
pure transformation over a payload dict.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import HydrationContext
from app.plugins.codex.service import _codex_command_hydrator

pytestmark = [pytest.mark.service]

# ── Helpers ───────────────────────────────────────────────────────────────────


_TEST_ORG_ID = uuid4()


def _make_payload(*, wallclock_seconds: int = 900) -> dict:
    """Minimal InvokeCodex command payload for hydrator input."""
    return {
        "kind": "InvokeCodex",
        "command_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "limits": {"wallclock_seconds": wallclock_seconds},
        "skill_path": ".codex/skills/test/SKILL.md",
    }


def _make_ctx(org_id: UUID | None = None) -> HydrationContext:
    return HydrationContext(org_id=org_id or _TEST_ORG_ID)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_key_mode_strips_org_id_no_auth_json(db_session: AsyncSession) -> None:
    """api_key mode: payload returned unchanged, no auth_json injected."""
    payload = _make_payload()
    ctx = _make_ctx()

    result = await _codex_command_hydrator(payload, ctx, db_session)

    assert "_org_id" not in result, "_org_id must not appear in the output"
    assert "auth_json" not in result, "api_key mode must not inject auth_json"
    assert result == payload
