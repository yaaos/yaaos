"""HTTP routes for cross-cutting system-readiness aggregation.

Only the onboarding aggregator lives here — it asks each registered plugin
"is your prereq satisfied?" via the `register_onboarding_contributor` registry
in `service.py`. Plugin-specific credential setters and per-plugin health
checks live under each plugin's own `/api/<plugin>/...` namespace.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from app.core.primitives import PluginMeta
from app.core.webserver import RouteSpec, register_routes
from app.domain.settings.service import OnboardingStatus, get_onboarding_status, list_plugins

M01_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

router = APIRouter()


@router.get("/onboarding")
async def onboarding() -> OnboardingStatus:
    return await get_onboarding_status(org_id=M01_ORG_ID)


@router.get("/plugins")
def plugins() -> list[PluginMeta]:
    """Discovery: every registered plugin's metadata. UI pairs each entry with
    its own `/api/<id>/health` for live status. Synchronous — registries are
    populated at bootstrap and reads are pure in-memory."""
    return list_plugins()


register_routes(RouteSpec(module_name="settings", router=router))
