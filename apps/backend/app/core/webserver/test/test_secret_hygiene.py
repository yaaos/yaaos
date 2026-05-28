"""Phase 13 — startup secret-hygiene check."""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def prod_env(monkeypatch):
    monkeypatch.setenv("YAAOS_ENV", "prod")
    monkeypatch.setenv("DATABASE_URL", os.environ.get("DATABASE_URL", "postgresql+asyncpg://x/y"))
    monkeypatch.setenv("YAAOS_ENCRYPTION_KEY", "VHJ5SW5nTm90VG9CcmVha1lvdXJTZWNyZXRzS2V5MTIzPQ==")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_prod_with_stub_secrets_raises(prod_env, monkeypatch):
    # Leave all auth-related secrets at their dev defaults.
    for var in (
        "YAAOS_GITHUB_APP_ID",
        "YAAOS_GITHUB_APP_SLUG",
        "YAAOS_GITHUB_APP_PRIVATE_KEY",
        "YAAOS_GITHUB_APP_WEBHOOK_SECRET",
        "YAAOS_GITHUB_OAUTH_CLIENT_ID",
        "YAAOS_GITHUB_OAUTH_CLIENT_SECRET",
        "YAAOS_TOTP_MASTER_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    from app.core.webserver.app_factory import _check_required_prod_secrets  # noqa: PLC0415

    with pytest.raises(RuntimeError, match="refuses to start in prod"):
        _check_required_prod_secrets()


def test_prod_with_all_secrets_set_does_not_raise(prod_env, monkeypatch):
    monkeypatch.setenv("YAAOS_OAUTH_STATE_SECRET", "real-state-secret")
    monkeypatch.setenv("YAAOS_INVITATION_TOKEN_SECRET", "real-invitation-secret")
    monkeypatch.setenv("YAAOS_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("YAAOS_GITHUB_APP_SLUG", "yaaos")
    monkeypatch.setenv(
        "YAAOS_GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nstub\n-----END PRIVATE KEY-----"
    )
    monkeypatch.setenv("YAAOS_GITHUB_APP_WEBHOOK_SECRET", "real-webhook-secret")
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_ID", "real-oauth-client-id")
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_SECRET", "real-oauth-client-secret")
    monkeypatch.setenv("YAAOS_TOTP_MASTER_KEY", "VHJ5SW5nTm90VG9CcmVha1lvdXJTZWNyZXRzS2V5MTIzPQ==")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    from app.core.webserver.app_factory import _check_required_prod_secrets  # noqa: PLC0415

    _check_required_prod_secrets()  # should not raise


def test_non_prod_skip_check(monkeypatch):
    monkeypatch.setenv("YAAOS_ENV", "dev")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    from app.core.webserver.app_factory import _check_required_prod_secrets  # noqa: PLC0415

    _check_required_prod_secrets()  # dev should be lenient
    get_settings.cache_clear()
