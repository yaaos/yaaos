"""HTTP routes owned by the claude_code plugin.

Plugin-owned URL namespace: credentials + health under `/api/claude_code/...`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import Action, public_route
from app.core.database import session as db_session
from app.core.sessions import require
from app.core.webserver import RouteSpec, register_routes
from app.plugins.claude_code.service import get_plugin

DEFAULT_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

# Default-deny: each route declares either `public_route` (unscoped endpoints) or `require(action)` (settings UI endpoints).
router = APIRouter()


@router.get("/health", dependencies=[Depends(public_route)])
async def health() -> dict[str, object]:
    h = await get_plugin().health_check()
    return {"healthy": h.healthy, "message": h.message, "checked_at": h.checked_at}


@router.get("/defaults", dependencies=[Depends(require(Action.CODING_AGENT_READ))])
async def defaults_endpoint() -> dict:
    """Model / effort dropdown enums for the Claude Code settings UI.
    Imported at request time so a code change surfaces on the next request."""
    from app.plugins.claude_code.defaults import EFFORTS, MODELS  # noqa: PLC0415

    return {"models": list(MODELS), "efforts": list(EFFORTS)}


# ── Per-repo skill name routes ────────────────────────────────────────────────


class RepoSkillRow(BaseModel):
    """One entry in the repos list — the repo identifier and its stored skill name."""

    repo_external_id: str
    skill_name: str | None


class SetRepoSkillRequest(BaseModel):
    skill_name: str | None = None


@router.get("/repos", dependencies=[Depends(require(Action.CODING_AGENT_READ))])
async def list_repos() -> dict[str, object]:
    """List repos connected to the org, joined with each repo's stored skill name.

    Joins the live GitHub repo list (from the VCS install) with stored
    `claude_code_repos` rows. Repos present in GitHub but absent from the DB
    are included with `skill_name=null`. Repos in the DB but absent from the
    GitHub list are omitted — the admin must reconnect via GitHub App settings.
    """
    from app.core import vcs as vcs_mod  # noqa: PLC0415
    from app.core.auth import org_id_var  # noqa: PLC0415
    from app.plugins.claude_code.repos import list_repos_with_skill  # noqa: PLC0415

    org_id = org_id_var.get() or DEFAULT_ORG_ID

    # Fetch the live repo list for the org's VCS install through core/vcs —
    # the github plugin owns repo enumeration; we never import it directly.
    try:
        github_repos = await vcs_mod.list_installation_repos("github", org_id)
    except Exception as e:
        return {"repos": [], "error": f"install repos: {e}"}

    # Read stored skill names from our table.
    async with db_session() as s:
        stored = await list_repos_with_skill(org_id, session=s)

    skill_by_repo = {r["repo_external_id"]: r["skill_name"] for r in stored}

    # Join: include only repos present in the live GitHub list.
    rows = [RepoSkillRow(repo_external_id=repo, skill_name=skill_by_repo.get(repo)) for repo in github_repos]
    return {"repos": [r.model_dump() for r in rows]}


@router.get("/repos/{repo_external_id:path}", dependencies=[Depends(require(Action.CODING_AGENT_READ))])
async def get_repo_skill(repo_external_id: str) -> RepoSkillRow:
    """Read the stored skill name for one repo.

    `repo_external_id` contains a `/` (`owner/repo`) so the path segment uses
    `:path` to avoid the `%2F`-decoded-before-routing 405 bug.
    """
    from app.core.auth import org_id_var  # noqa: PLC0415
    from app.plugins.claude_code.repos import resolve_skill  # noqa: PLC0415

    org_id = org_id_var.get() or DEFAULT_ORG_ID
    async with db_session() as s:
        skill_name = await resolve_skill(org_id, repo_external_id, session=s)
    return RepoSkillRow(repo_external_id=repo_external_id, skill_name=skill_name)


@router.put("/repos/{repo_external_id:path}", dependencies=[Depends(require(Action.CODING_AGENT_WRITE))])
async def set_repo_skill_route(repo_external_id: str, req: SetRepoSkillRequest) -> RepoSkillRow:
    """Write the skill name for one repo. Creates the identity row if absent.

    `repo_external_id` contains a `/` (`owner/repo`) so the path segment uses
    `:path` to avoid the `%2F`-decoded-before-routing 405 bug.
    """
    from app.core.auth import org_id_var  # noqa: PLC0415
    from app.plugins.claude_code.repos import set_repo_skill  # noqa: PLC0415

    org_id = org_id_var.get() or DEFAULT_ORG_ID
    async with db_session() as s:
        await set_repo_skill(org_id, repo_external_id, req.skill_name, session=s)
        await s.commit()
    return RepoSkillRow(repo_external_id=repo_external_id, skill_name=req.skill_name or None)


register_routes(RouteSpec(module_name="claude_code", router=router))
