"""HTTP wiring for BYOK.

| Method | Path                              | Action       |
|--------|-----------------------------------|--------------|
| GET    | `/api/api-keys`                       | `BYOK_READ`  — list providers with status (configured / not_set) + timestamps. |
| POST   | `/api/api-keys/{provider}`            | `BYOK_WRITE` — set/update the encrypted key. |
| POST   | `/api/api-keys/{provider}/validate`   | `BYOK_WRITE` — call the provider plugin's validator with the stored key. |
| DELETE | `/api/api-keys/{provider}`            | `BYOK_WRITE` — remove the row. |

Plaintext never crosses the API boundary except inbound on `POST {provider}`.
GET returns "configured" / "not_set" only.

Org context via `X-Yaaos-Org-Slug`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel

from app.core import byok as byok_service
from app.core.auth import Action, org_id_var
from app.core.database import session as db_session
from app.core.sessions import current_actor, require
from app.core.webserver import RouteSpec, register_routes

log = structlog.get_logger("byok.web")

router = APIRouter()


# Providers the UI surfaces — sourced from the validator registry so adding
# a new provider plugin (registering its validator at bootstrap) auto-surfaces
# it here. Plugins register via `byok_service.register_validator(provider, ...)`.
def _known_providers() -> tuple[str, ...]:
    return tuple(byok_service.known_providers())


class ProviderStatus(BaseModel):
    provider: str
    status: str  # "configured" | "not_set"
    last_validated_at: datetime | None
    last_used_at: datetime | None
    updated_at: datetime | None


class SetKeyRequest(BaseModel):
    value: str


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


@router.get("", dependencies=[Depends(require(Action.BYOK_READ))])
async def list_providers() -> list[ProviderStatus]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        keys = {k.provider: k for k in await byok_service.list_keys_for_org(org_id, session=s)}
    out: list[ProviderStatus] = []
    for prov in _known_providers():
        k = keys.get(prov)
        out.append(
            ProviderStatus(
                provider=prov,
                status="configured" if k is not None else "not_set",
                last_validated_at=k.last_validated_at if k else None,
                last_used_at=k.last_used_at if k else None,
                updated_at=k.updated_at if k else None,
            )
        )
    return out


@router.post("/{provider}", dependencies=[Depends(require(Action.BYOK_WRITE))])
async def set_key(
    provider: Annotated[str, Path()],
    body: SetKeyRequest,
) -> dict[str, str]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    if provider not in _known_providers():
        raise _err(404, "unknown_provider")
    if not body.value:
        raise _err(422, "empty_value")
    actor = current_actor()
    async with db_session() as s:
        await byok_service.set(org_id, provider, body.value, actor=actor, session=s)
        await s.commit()
    return {"status": "configured"}


@router.post("/{provider}/validate", dependencies=[Depends(require(Action.BYOK_WRITE))])
async def validate_key(
    provider: Annotated[str, Path()],
) -> dict[str, bool]:
    """Calls the plugin-supplied validator from the registry. Stamps
    `last_validated_at` on success. Provider-specific HTTP lives in the
    plugin, not here."""
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    validator = byok_service.get_validator(provider)
    if validator is None:
        raise _err(404, "unknown_provider")
    async with db_session() as s:
        ok = await byok_service.validate(org_id, provider, validator, actor=actor, session=s)
        await s.commit()
    return {"valid": ok}


@router.delete("/{provider}", dependencies=[Depends(require(Action.BYOK_WRITE))])
async def clear_key(provider: Annotated[str, Path()]) -> dict[str, bool]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    if provider not in _known_providers():
        raise _err(404, "unknown_provider")
    actor = current_actor()
    async with db_session() as s:
        removed = await byok_service.clear(org_id, provider, actor=actor, session=s)
        await s.commit()
    return {"removed": removed}


register_routes(RouteSpec(module_name="byok", router=router, url_prefix="/api/api-keys"))
