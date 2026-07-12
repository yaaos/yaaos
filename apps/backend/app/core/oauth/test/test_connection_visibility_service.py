"""Service tests for `list_visible_user_oauth_apps` — relevance-gated provider
visibility on the user connections surface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity import create_user
from app.core.oauth import (
    UserOAuthApp,
    list_visible_user_oauth_apps,
    register_user_oauth_app,
)
from app.core.oauth.models import UserOAuthConnectionRow
from app.core.oauth.user_connections import _APPS

_PROVIDER_ID = "test_visibility_provider"


def _make_app(relevance_result: bool | None) -> UserOAuthApp:
    async def _relevance(user_id: UUID, session: AsyncSession) -> bool:
        assert relevance_result is not None
        return relevance_result

    return UserOAuthApp(
        provider_id=_PROVIDER_ID,
        display_name="Visibility Test Provider",
        connect_hint="test hint",
        flow="device_code",
        device_authorize_url="http://fake-vis.test/device/code",
        token_url="http://fake-vis.test/token",
        client_id="vis-client-id",
        client_secret=None,
        default_scopes=("read",),
        token_auth_style="form",
        scope_separator=" ",
        expiry_source="expires_in",
        capture_id_token=False,
        account_id_extractor=None,
        refresh_after_seconds=345600,
        relevance_fn=None if relevance_result is None else _relevance,
    )


@pytest.fixture(autouse=True)
def _clear_test_app() -> None:
    _APPS.pop(_PROVIDER_ID, None)


@pytest_asyncio.fixture
async def user_id(db_session: AsyncSession) -> UUID:
    user = await create_user(db_session, display_name="Visibility Test User")
    return user.id


def _provider_ids(apps: list[UserOAuthApp]) -> set[str]:
    return {a.provider_id for a in apps}


@pytest.mark.service
@pytest.mark.asyncio
async def test_app_without_relevance_fn_is_visible(db_session: AsyncSession, user_id: UUID) -> None:
    register_user_oauth_app(_make_app(relevance_result=None))
    apps = await list_visible_user_oauth_apps(user_id, session=db_session)
    assert _PROVIDER_ID in _provider_ids(apps)


@pytest.mark.service
@pytest.mark.asyncio
async def test_irrelevant_app_is_hidden(db_session: AsyncSession, user_id: UUID) -> None:
    register_user_oauth_app(_make_app(relevance_result=False))
    apps = await list_visible_user_oauth_apps(user_id, session=db_session)
    assert _PROVIDER_ID not in _provider_ids(apps)


@pytest.mark.service
@pytest.mark.asyncio
async def test_relevant_app_is_visible(db_session: AsyncSession, user_id: UUID) -> None:
    register_user_oauth_app(_make_app(relevance_result=True))
    apps = await list_visible_user_oauth_apps(user_id, session=db_session)
    assert _PROVIDER_ID in _provider_ids(apps)


@pytest.mark.service
@pytest.mark.asyncio
async def test_irrelevant_app_with_existing_connection_stays_visible(
    db_session: AsyncSession, user_id: UUID
) -> None:
    """An existing connection row keeps the card visible so the user can still
    disconnect after the provider stops being relevant."""
    register_user_oauth_app(_make_app(relevance_result=False))
    db_session.add(
        UserOAuthConnectionRow(
            user_id=user_id,
            provider_id=_PROVIDER_ID,
            status="connected",
            encrypted_access_token="x",
            encrypted_refresh_token=None,
            encrypted_id_token=None,
            external_account_id=None,
            access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            last_refresh_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    apps = await list_visible_user_oauth_apps(user_id, session=db_session)
    assert _PROVIDER_ID in _provider_ids(apps)
