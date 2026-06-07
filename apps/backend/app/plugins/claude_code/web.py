"""HTTP routes owned by the claude_code plugin.

Plugin-owned URL namespace: credentials + health under `/api/claude_code/...`
and skill-manifest CRUD under `/api/claude_code/repos/{repo_external_id}/...`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, SecretStr

from app.core.auth import Action, current_org_id, public_route
from app.core.sessions import require
from app.core.webserver import RouteSpec, register_routes
from app.plugins.claude_code.service import _set_anthropic_key, bootstrap_anthropic_env, get_plugin

DEFAULT_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

# Default-deny: each route declares either `public_route` (# unscoped setup endpoints) or `require(action)` (settings UI endpoints).
router = APIRouter()


class SetApiKeyRequest(BaseModel):
    api_key: SecretStr


@router.post("/api_key", dependencies=[Depends(public_route)])
async def set_api_key(req: SetApiKeyRequest) -> dict[str, str]:
    if not req.api_key.get_secret_value().strip():
        raise HTTPException(status_code=400, detail={"api_key": "must not be empty"})
    await _set_anthropic_key(DEFAULT_ORG_ID, req.api_key)
    return {"status": "saved"}


@router.get("/health", dependencies=[Depends(public_route)])
async def health() -> dict[str, object]:
    h = await get_plugin().health_check()
    return {"healthy": h.healthy, "message": h.message, "checked_at": h.checked_at}


@router.get("/defaults", dependencies=[Depends(require(Action.CODING_AGENT_READ))])
async def defaults_endpoint() -> dict:
    """Code defaults for the orchestrator + sub-agents, plus the model /
    version / effort dropdown enums. Imported at request time so a code
    change to `defaults.py` surfaces on the next request — never cached."""
    from app.plugins.claude_code.defaults import get_defaults  # noqa: PLC0415

    return get_defaults()


# ── Skill manifest endpoints ─────────────────────────────────────────────


class SkillRefreshResponse(BaseModel):
    ticket_id: str
    created: bool


@router.post(
    "/repos/{repo_external_id}/skills/refresh",
    dependencies=[Depends(require(Action.CODING_AGENT_WRITE))],
)
async def refresh_skills(repo_external_id: str) -> SkillRefreshResponse:
    """Kick off skill enumeration for a repo. Creates a system ticket and starts
    the `enumerate_skills_v1` workflow. Returns immediately — the manifest update
    arrives via the `skills_enumerated` SSE event."""
    from app.core.database import session as db_session  # noqa: PLC0415
    from app.core.workflow import get_engine  # noqa: PLC0415
    from app.domain.tickets import create as create_ticket  # noqa: PLC0415

    org_id = current_org_id()
    plugin_id = "github"

    idempotency_key = f"skill_enum:{org_id}:{repo_external_id}"
    title = f"System · Skill enumeration · {repo_external_id}"

    async with db_session() as s:
        ticket_id, created = await create_ticket(
            type="skill_enumeration",
            payload={"repo_full_name": repo_external_id, "plugin_id": plugin_id, "head_sha": "HEAD"},
            idempotency_key=idempotency_key,
            org_id=org_id,
            title=title,
            source="system",
            repo_external_id=repo_external_id,
            plugin_id=plugin_id,
            session=s,
        )
        engine = get_engine()
        await engine.start(
            workflow_name="enumerate_skills_v1",
            ticket_id=str(ticket_id),
            session=s,
        )
        await s.commit()

    return SkillRefreshResponse(ticket_id=str(ticket_id), created=created)


@router.get(
    "/repos/{repo_external_id}/skills",
    dependencies=[Depends(require(Action.CODING_AGENT_READ))],
)
async def get_skills(repo_external_id: str) -> list[dict]:
    """Return the cached skill manifest for a repo. Empty list if not yet enumerated."""
    from app.core.database import session as db_session  # noqa: PLC0415
    from app.plugins.claude_code.repos import get_skill_manifest  # noqa: PLC0415

    org_id = current_org_id()
    async with db_session() as s:
        skills = await get_skill_manifest(org_id, repo_external_id, session=s)
    return [s.model_dump() for s in skills]


register_routes(
    RouteSpec(
        module_name="claude_code",
        router=router,
        on_startup=[bootstrap_anthropic_env],
    )
)
