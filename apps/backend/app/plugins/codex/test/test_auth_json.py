"""Unit tests for `plugins/codex/auth_json.py`."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from app.core.oauth import UserOAuthCredential
from app.plugins.codex.auth_json import build_auth_json


def _make_cred(
    *,
    access_token: str = "at.123",
    id_token: str | None = "it.456",
    account_id: str | None = "acct-abc",
) -> UserOAuthCredential:
    return UserOAuthCredential(
        access_token=SecretStr(access_token),
        id_token=SecretStr(id_token) if id_token is not None else None,
        external_account_id=account_id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_build_auth_json_returns_secret_str() -> None:
    """Result is a SecretStr — plaintext not leaked in repr/logs."""
    result = build_auth_json(_make_cred())
    assert isinstance(result, SecretStr)
    # SecretStr redacts in repr
    assert "at.123" not in repr(result)


def test_build_auth_json_exact_shape() -> None:
    """Payload matches the chatgptAuthTokens schema exactly."""
    cred = _make_cred(access_token="access-token-xyz", id_token="id-token-xyz", account_id="acct-99")
    raw = json.loads(build_auth_json(cred).get_secret_value())

    assert raw["auth_mode"] == "chatgptAuthTokens"
    assert raw["tokens"]["access_token"] == "access-token-xyz"
    assert raw["tokens"]["id_token"] == "id-token-xyz"
    assert raw["tokens"]["account_id"] == "acct-99"
    # refresh_token is always empty — backend owns the refresh cycle
    assert raw["tokens"]["refresh_token"] == ""
    # last_refresh is an ISO-8601 UTC timestamp
    last_refresh = datetime.fromisoformat(raw["last_refresh"])
    assert last_refresh.tzinfo is not None
    assert abs((last_refresh - datetime.now(UTC)).total_seconds()) < 5


def test_build_auth_json_empty_id_token_when_none() -> None:
    """When `id_token` is None, `tokens.id_token` is an empty string."""
    cred = _make_cred(id_token=None)
    raw = json.loads(build_auth_json(cred).get_secret_value())
    assert raw["tokens"]["id_token"] == ""


def test_build_auth_json_empty_account_id_when_none() -> None:
    """When `external_account_id` is None, `tokens.account_id` is an empty string."""
    cred = _make_cred(account_id=None)
    raw = json.loads(build_auth_json(cred).get_secret_value())
    assert raw["tokens"]["account_id"] == ""
