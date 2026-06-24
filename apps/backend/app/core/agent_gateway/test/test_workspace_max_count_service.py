"""Service test: `_build_config_update_dto` carries the org's
`workspace_max_count` onto the outgoing `AgentConfig.max_workspaces`.

Intra-module test — lives inside `core/agent_gateway/test/` so private reach
into `service._build_config_update_dto` is permitted.
"""

from __future__ import annotations

import pytest

from app.core.tenancy import update_org_fields
from app.domain.orgs import insert_org


@pytest.mark.asyncio
@pytest.mark.service
async def test_build_config_update_uses_org_workspace_max_count(db_session) -> None:
    """`_build_config_update_dto` reads `workspace_max_count` from `OrgFullView`."""
    import app.core.agent_gateway.service as svc  # noqa: PLC0415

    org = await insert_org(db_session, slug="wmc-test")
    await update_org_fields(db_session, org.org_id, workspace_max_count=12)
    await db_session.commit()

    cmd = await svc._build_config_update_dto(org.org_id, session=db_session)
    assert cmd.config.max_workspaces == 12


@pytest.mark.asyncio
@pytest.mark.service
async def test_build_config_update_defaults_to_column_default(db_session) -> None:
    """A freshly seeded org carries `workspace_max_count=4` (column server default)
    and `_build_config_update_dto` propagates that to `AgentConfig.max_workspaces`."""
    import app.core.agent_gateway.service as svc  # noqa: PLC0415

    org = await insert_org(db_session, slug="wmc-default")
    await db_session.commit()

    cmd = await svc._build_config_update_dto(org.org_id, session=db_session)
    assert cmd.config.max_workspaces == 4
