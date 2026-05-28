"""Service test for ``claude_code.set_api_key``."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.config import get_settings
from app.domain.orgs import create_org
from app.plugins.claude_code import set_api_key
from app.plugins.claude_code.models import ClaudeCodeSettingsRow


def _encrypt(plaintext: bytes) -> bytes:
    fernet = Fernet(get_settings().yaaos_encryption_key.get_secret_value().encode())
    return fernet.encrypt(plaintext)


@pytest.mark.asyncio
@pytest.mark.service
async def test_set_api_key_inserts_row(db_session) -> None:
    """First call inserts a new ``claude_code_settings`` row."""
    org = await create_org(db_session, slug="cc-key-org-1", display_name="CC Key Org 1")
    enc_key = _encrypt(b"sk-ant-first-key")

    await set_api_key(db_session, org_id=org.id, encrypted_anthropic_api_key=enc_key)
    await db_session.commit()

    row = (
        await db_session.execute(select(ClaudeCodeSettingsRow).where(ClaudeCodeSettingsRow.org_id == org.id))
    ).scalar_one_or_none()

    assert row is not None
    assert row.encrypted_anthropic_api_key == enc_key


@pytest.mark.asyncio
@pytest.mark.service
async def test_set_api_key_second_call_updates_not_duplicates(db_session) -> None:
    """Second call with a new value updates the row; no duplicate is created."""
    org = await create_org(db_session, slug="cc-key-org-2", display_name="CC Key Org 2")
    enc_key_1 = _encrypt(b"sk-ant-first")
    enc_key_2 = _encrypt(b"sk-ant-second")

    await set_api_key(db_session, org_id=org.id, encrypted_anthropic_api_key=enc_key_1)
    await db_session.commit()
    await set_api_key(db_session, org_id=org.id, encrypted_anthropic_api_key=enc_key_2)
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(ClaudeCodeSettingsRow).where(ClaudeCodeSettingsRow.org_id == org.id)
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 1
    assert rows[0].encrypted_anthropic_api_key == enc_key_2
