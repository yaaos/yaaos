"""Tests for app_mode (APP_MODE) and environment (ENVIRONMENT) Settings fields.

Covers:
- is_dev / is_test / is_production / is_non_prod accessors
- rate-limit + SlowAPI are active only when is_production
- environment is independent of app_mode
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app.core.config.service import get_settings


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate all required Settings fields. Individual tests override as needed."""
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get("DATABASE_URL", "postgresql+asyncpg://yaaos:yaaos@localhost:5432/yaaos_test"),
    )
    monkeypatch.setenv("YAAOS_ENCRYPTION_KEY", "VHJ5SW5nTm90VG9CcmVha1lvdXJTZWNyZXRzS2V5MTIzPQ==")
    monkeypatch.setenv("REDIS_URL", os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    monkeypatch.setenv("YAAOS_PUBLIC_ORIGIN", "https://app.yaaos.dev")
    monkeypatch.setenv("ENVIRONMENT", "test")
    # conftest sets YAAOS_CODING_AGENT_STUB=1 process-wide for the suite; clear it
    # here so the prod-mode tests below present a coherent production env (the
    # model validator forbids the stub flag under APP_MODE=production).
    monkeypatch.delenv("YAAOS_CODING_AGENT_STUB", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── app_mode accessors ──────────────────────────────────────────────────────


def test_is_production_true_when_app_mode_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "production")
    get_settings.cache_clear()
    s = get_settings()
    assert s.app_mode == "production"
    assert s.is_production is True
    assert s.is_non_prod is False
    assert s.is_dev is False
    assert s.is_test is False


def test_is_dev_true_when_app_mode_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "dev")
    get_settings.cache_clear()
    s = get_settings()
    assert s.is_dev is True
    assert s.is_production is False
    assert s.is_non_prod is True


def test_is_test_true_when_app_mode_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "test")
    get_settings.cache_clear()
    s = get_settings()
    assert s.is_test is True
    assert s.is_production is False
    assert s.is_non_prod is True


def test_default_app_mode_is_production() -> None:
    """Without APP_MODE set the default must be 'production' (safe in prod containers)."""
    import os as _os  # noqa: PLC0415

    saved = _os.environ.pop("APP_MODE", None)
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.app_mode == "production"
    finally:
        if saved is not None:
            _os.environ["APP_MODE"] = saved
        get_settings.cache_clear()


# ── rate-limit active only in is_production ─────────────────────────────────


@pytest.mark.service
def test_slowapi_middleware_active_when_is_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """SlowAPI middleware must be installed when app_mode=production."""
    monkeypatch.setenv("APP_MODE", "production")
    # Provide all secrets required to pass _check_required_prod_secrets
    monkeypatch.setenv("YAAOS_OAUTH_STATE_SECRET", "real-state-secret")
    monkeypatch.setenv("YAAOS_INVITATION_TOKEN_SECRET", "real-invitation-secret")
    monkeypatch.setenv("YAAOS_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("YAAOS_GITHUB_APP_SLUG", "yaaos")
    monkeypatch.setenv(
        "YAAOS_GITHUB_APP_PRIVATE_KEY",
        "-----BEGIN PRIVATE KEY-----\nstub\n-----END PRIVATE KEY-----",
    )
    monkeypatch.setenv("YAAOS_GITHUB_APP_WEBHOOK_SECRET", "real-webhook-secret")
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_ID", "real-client-id")
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_SECRET", "real-client-secret")
    monkeypatch.setenv("YAAOS_TOTP_MASTER_KEY", "VHJ5SW5nTm90VG9CcmVha1lvdXJTZWNyZXRzS2V5MTIzPQ==")
    monkeypatch.setenv("YAAOS_CLOUDFLARE_INGRESS_SECRET", "real-cf-ingress-secret")
    get_settings.cache_clear()

    from app.core.webserver import create_app  # noqa: PLC0415

    app = create_app()
    app_state_has_limiter = hasattr(app.state, "limiter") and app.state.limiter is not None
    assert app_state_has_limiter, "SlowAPI limiter must be installed on app.state when is_production"


@pytest.mark.service
def test_slowapi_middleware_not_active_when_is_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """SlowAPI middleware must NOT be installed in dev."""
    monkeypatch.setenv("APP_MODE", "dev")
    get_settings.cache_clear()

    from app.core.webserver import create_app  # noqa: PLC0415

    app = create_app()
    assert not hasattr(app.state, "limiter") or app.state.limiter is None, (
        "SlowAPI limiter must not be installed when app_mode=dev"
    )


# ── environment is independent of app_mode ─────────────────────────────────


def test_environment_unset_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """ENVIRONMENT must be supplied explicitly — no default.

    Without it, Settings refuses to construct. A new deploy that forgets to
    set the var fails-fast at boot rather than silently tagging telemetry
    with a wrong-tier default.
    """
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="environment"):
        get_settings()


def test_environment_accepts_arbitrary_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """No literal whitelist — a new tier name (e.g. 'staging-eu') works without a code change."""
    monkeypatch.setenv("ENVIRONMENT", "staging-eu")
    get_settings.cache_clear()
    s = get_settings()
    assert s.environment == "staging-eu"


def test_environment_read_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    get_settings.cache_clear()
    s = get_settings()
    assert s.environment == "staging"


def test_environment_independent_of_app_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """app_mode=production + environment=staging must both hold simultaneously.

    A staging deploy runs prod-like behavior but reports a different
    deployment tier for telemetry.
    """
    monkeypatch.setenv("APP_MODE", "production")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    get_settings.cache_clear()
    s = get_settings()
    assert s.app_mode == "production"
    assert s.is_production is True
    assert s.environment == "staging"
    assert s.environment != s.app_mode


def test_environment_production_with_app_mode_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "production")
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    s = get_settings()
    assert s.app_mode == "production"
    assert s.environment == "production"


# ── STS host override forbidden in production ────────────────────────────────


def test_sts_host_override_in_production_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """APP_MODE=production + YAAOS_STS_HOST_OVERRIDE set → Settings refuses to construct.

    A prod deployment must never replay agent identity against a mock STS.
    """
    monkeypatch.setenv("APP_MODE", "production")
    monkeypatch.setenv("YAAOS_STS_HOST_OVERRIDE", "mock-aws:4566")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="YAAOS_STS_HOST_OVERRIDE"):
        get_settings()


def test_sts_host_override_allowed_in_non_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "test")
    monkeypatch.setenv("YAAOS_STS_HOST_OVERRIDE", "mock-aws:4566")
    get_settings.cache_clear()
    s = get_settings()
    assert s.yaaos_sts_host_override == "mock-aws:4566"


def test_production_without_sts_host_override_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "production")
    monkeypatch.delenv("YAAOS_STS_HOST_OVERRIDE", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.is_production is True
    assert s.yaaos_sts_host_override is None


# ── Stub switches forbidden in production ────────────────────────────────────


def test_coding_agent_stub_in_production_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """APP_MODE=production + YAAOS_CODING_AGENT_STUB → Settings refuses to construct.

    A prod deployment that stubbed the coding agent would silently fake reviews.
    """
    monkeypatch.setenv("APP_MODE", "production")
    monkeypatch.setenv("YAAOS_CODING_AGENT_STUB", "1")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="YAAOS_CODING_AGENT_STUB"):
        get_settings()


def test_pr_comment_classifier_stub_in_production_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "production")
    monkeypatch.setenv("YAAOS_PR_COMMENT_CLASSIFIER_STUB", "1")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="YAAOS_PR_COMMENT_CLASSIFIER_STUB"):
        get_settings()


def test_multiple_non_prod_only_settings_all_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validator collects every offender into one message, not just the first."""
    monkeypatch.setenv("APP_MODE", "production")
    monkeypatch.setenv("YAAOS_STS_HOST_OVERRIDE", "mock-aws:4566")
    monkeypatch.setenv("YAAOS_CODING_AGENT_STUB", "1")
    monkeypatch.setenv("YAAOS_PR_COMMENT_CLASSIFIER_STUB", "1")
    get_settings.cache_clear()
    with pytest.raises(ValidationError) as exc:
        get_settings()
    msg = str(exc.value)
    assert "YAAOS_STS_HOST_OVERRIDE" in msg
    assert "YAAOS_CODING_AGENT_STUB" in msg
    assert "YAAOS_PR_COMMENT_CLASSIFIER_STUB" in msg


def test_stub_switches_allowed_in_non_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "test")
    monkeypatch.setenv("YAAOS_CODING_AGENT_STUB", "1")
    monkeypatch.setenv("YAAOS_PR_COMMENT_CLASSIFIER_STUB", "1")
    get_settings.cache_clear()
    s = get_settings()
    assert s.yaaos_coding_agent_stub is True
    assert s.yaaos_pr_comment_classifier_stub is True
