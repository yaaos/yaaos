"""Every config secret is a Pydantic SecretStr — repr + model_dump redact.

If a new sensitive field lands as a bare `str`, this test catches it
before it leaks into a log line, traceback, or audit JSON dump.
"""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from app.core.config.service import Settings, get_settings

# Every field listed here MUST be SecretStr (or SecretStr | None). The
# list is intentionally explicit — adding a new secret means adding it
# both to Settings and here, which makes "did I remember to redact"
# obvious in code review.
_SECRET_FIELDS = (
    "yaaos_encryption_key",
    "redis_url",  # not a SecretStr today, but if we ever auth Redis with
    # an inline password (redis://user:pw@host) it should be — leave the
    # entry commented to remind reviewers. Uncomment if/when wrapped.
)

_EXPECTED_SECRET_TYPES = {
    "yaaos_encryption_key",
    "braintrust_api_key",
    "yaaos_github_app_private_key",
    "yaaos_github_app_webhook_secret",
    "yaaos_github_oauth_client_secret",
    "yaaos_oauth_state_secret",
    "yaaos_totp_master_key",
    "yaaos_oauth_linear_client_secret",
    "yaaos_oauth_notion_client_secret",
    "yaaos_invitation_token_secret",
    "smtp_password",
}


@pytest.fixture(autouse=True)
def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings construction needs all required fields populated."""
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get("DATABASE_URL", "postgresql+asyncpg://yaaos:yaaos@localhost:5432/yaaos_test"),
    )
    monkeypatch.setenv("YAAOS_ENCRYPTION_KEY", "SUPER-SECRET-ENCRYPTION-KEY-DO-NOT-LEAK")
    monkeypatch.setenv("REDIS_URL", os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    monkeypatch.setenv("YAAOS_PUBLIC_HOSTNAME", "app.yaaos.cloud")
    monkeypatch.setenv("YAAOS_GITHUB_OAUTH_CLIENT_SECRET", "SUPER-SECRET-GH-OAUTH-SECRET")
    monkeypatch.setenv("YAAOS_GITHUB_APP_PRIVATE_KEY", "SUPER-SECRET-GH-PRIVATE-KEY")
    monkeypatch.setenv("YAAOS_GITHUB_APP_WEBHOOK_SECRET", "SUPER-SECRET-GH-WEBHOOK-SECRET")
    monkeypatch.setenv("YAAOS_TOTP_MASTER_KEY", "SUPER-SECRET-TOTP-MASTER-KEY")
    monkeypatch.setenv("SMTP_PASSWORD", "SUPER-SECRET-SMTP-PASSWORD")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_known_sensitive_fields_are_secret_str() -> None:
    """Every field we've identified as sensitive must declare SecretStr.
    Failures here mean a new secret field landed as `str` — wrap it
    before merging."""
    settings = get_settings()
    for name in _EXPECTED_SECRET_TYPES:
        value = getattr(settings, name)
        assert value is None or isinstance(value, SecretStr), (
            f"Settings.{name} must be SecretStr (or None), got {type(value).__name__}"
        )


def test_repr_does_not_expose_secret_plaintexts() -> None:
    """`repr(settings)` is the path through which secrets most often
    leak — log lines, exception tracebacks, structured-log field dumps.
    SecretStr renders as '**********', not the plaintext."""
    settings = get_settings()
    rendered = repr(settings)
    for plaintext in (
        "SUPER-SECRET-ENCRYPTION-KEY-DO-NOT-LEAK",
        "SUPER-SECRET-GH-OAUTH-SECRET",
        "SUPER-SECRET-GH-PRIVATE-KEY",
        "SUPER-SECRET-GH-WEBHOOK-SECRET",
        "SUPER-SECRET-TOTP-MASTER-KEY",
        "SUPER-SECRET-SMTP-PASSWORD",
    ):
        assert plaintext not in rendered, f"plaintext leaked in repr: {plaintext!r}"


def test_model_dump_does_not_expose_secret_plaintexts() -> None:
    """`model_dump_json()` and `model_dump()` both must redact secrets.
    Some audit / telemetry paths serialize whole settings objects."""
    settings = get_settings()
    json_rendered = settings.model_dump_json()
    dict_rendered = str(settings.model_dump())
    for plaintext in (
        "SUPER-SECRET-ENCRYPTION-KEY-DO-NOT-LEAK",
        "SUPER-SECRET-GH-OAUTH-SECRET",
        "SUPER-SECRET-GH-PRIVATE-KEY",
        "SUPER-SECRET-GH-WEBHOOK-SECRET",
        "SUPER-SECRET-TOTP-MASTER-KEY",
        "SUPER-SECRET-SMTP-PASSWORD",
    ):
        assert plaintext not in json_rendered, f"plaintext leaked in model_dump_json: {plaintext!r}"
        assert plaintext not in dict_rendered, f"plaintext leaked in model_dump: {plaintext!r}"


def test_get_secret_value_returns_plaintext_when_explicitly_requested() -> None:
    """`.get_secret_value()` is the documented escape hatch — used at
    the byte boundary (Fernet encrypt, JWT sign, HTTP Authorization)."""
    settings = get_settings()
    assert settings.yaaos_encryption_key.get_secret_value() == "SUPER-SECRET-ENCRYPTION-KEY-DO-NOT-LEAK"
    assert settings.smtp_password.get_secret_value() == "SUPER-SECRET-SMTP-PASSWORD"


def test_settings_class_lists_every_known_secret() -> None:
    """Cross-check: every name in `_EXPECTED_SECRET_TYPES` must still
    exist on the Settings class. Drift here means we deleted a field
    without updating this test."""
    field_names = set(Settings.model_fields.keys())
    missing = _EXPECTED_SECRET_TYPES - field_names
    assert not missing, f"Settings is missing fields this test expects: {sorted(missing)}"


def test_settings_fails_fast_when_yaaos_public_hostname_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """YAAOS_PUBLIC_HOSTNAME is required — Settings construction must raise
    when it is absent, not silently default to an empty string."""
    from pydantic import ValidationError  # noqa: PLC0415

    monkeypatch.delenv("YAAOS_PUBLIC_HOSTNAME", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        Settings()
    get_settings.cache_clear()
