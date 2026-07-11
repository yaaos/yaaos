"""Service test: build_api_key_secrets_for_org returns every stored org key.

Forward-all builder: iterates `list_keys_for_org` then `get` per provider.
Agent-side env maps are the allowlist; unknown providers are ignored there.
"""

from __future__ import annotations

import pytest

import app.core.api_keys as api_keys
from app.core.audit_log import Actor
from app.core.auth import Role
from app.core.coding_agent.api_keys import build_api_key_secrets_for_org
from app.core.identity import create_user
from app.domain.orgs import insert_membership, insert_org


@pytest.mark.asyncio
@pytest.mark.service
async def test_build_returns_all_stored_keys(db_session) -> None:
    """Org with anthropic + rwx keys stored → both present in the returned dict."""
    user = await create_user(db_session, display_name="U")
    org = await insert_org(db_session, slug="fwd-all-keys")
    await insert_membership(db_session, user_id=user.id, org_id=org.org_id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    await api_keys.set(org.org_id, "anthropic", "sk-ant-secret", actor=actor, session=db_session)
    await api_keys.set(org.org_id, "rwx", "rwx-token-secret", actor=actor, session=db_session)

    result = await build_api_key_secrets_for_org(org.org_id, session=db_session)

    assert set(result.keys()) == {"anthropic", "rwx"}
    assert result["anthropic"].get_secret_value() == "sk-ant-secret"
    assert result["rwx"].get_secret_value() == "rwx-token-secret"


@pytest.mark.asyncio
@pytest.mark.service
async def test_build_returns_empty_dict_when_no_keys(db_session) -> None:
    """Org with no stored keys → empty dict."""
    org = await insert_org(db_session, slug="fwd-all-empty")

    result = await build_api_key_secrets_for_org(org.org_id, session=db_session)

    assert result == {}


@pytest.mark.asyncio
@pytest.mark.service
async def test_build_returns_single_key(db_session) -> None:
    """Org with only anthropic stored → only anthropic in the result."""
    user = await create_user(db_session, display_name="U")
    org = await insert_org(db_session, slug="fwd-all-single")
    await insert_membership(db_session, user_id=user.id, org_id=org.org_id, role=Role.OWNER, handle="u")
    actor = Actor.user(user_id=user.id)

    await api_keys.set(org.org_id, "anthropic", "sk-only", actor=actor, session=db_session)

    result = await build_api_key_secrets_for_org(org.org_id, session=db_session)

    assert list(result.keys()) == ["anthropic"]
    assert result["anthropic"].get_secret_value() == "sk-only"
