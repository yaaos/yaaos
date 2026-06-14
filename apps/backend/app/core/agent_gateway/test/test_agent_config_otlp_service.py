"""Service tests for AgentConfig OTLP field types and _build_config_update.

Covers:
- `_build_config_update` populates otlp_endpoint, otlp_dataset, and otlp_token
  from settings; the wire JSON carries the token in plaintext (via the
  field_serializer) while model_dump / str / repr redact it.
- `AgentConfig.otlp_token` shows [REDACTED] in str(), repr(), and model_dump().
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr


@pytest.mark.service
def test_build_config_update_populates_otlp_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_config_update carries endpoint + dataset as-is and exposes the
    bearer token ONLY when serialized to JSON (the wire-encode boundary).

    Tested by:
    1. Setting YAAOS_DASH0_* env vars + YAAOS_AGENT_DASH0_BEARER_TOKEN.
    2. Calling _build_config_update() with a fresh Settings instance.
    3. Asserting model_dump(mode='json') on the nested config carries the raw token.
    4. Asserting model_dump() (Python mode) keeps it redacted.
    """
    import app.core.agent_gateway.service as svc  # noqa: PLC0415
    from app.core.config import get_settings  # noqa: PLC0415

    monkeypatch.setenv("YAAOS_DASH0_ENDPOINT", "https://ingress.us-west-2.aws.dash0.com")
    monkeypatch.setenv("YAAOS_DASH0_DATASET", "default")
    monkeypatch.setenv("YAAOS_AGENT_DASH0_BEARER_TOKEN", "agent-bearer-xyz")

    # Clear the @cache on get_settings() so it picks up the patched env vars.
    get_settings.cache_clear()
    try:
        cmd = svc._build_config_update()
        config = cmd.config

        # Python mode: token must be redacted.
        py_dump = config.model_dump()
        assert py_dump["otlp_token"] != "agent-bearer-xyz", (
            "otlp_token must be redacted in model_dump() (Python mode)"
        )

        # JSON mode (wire-encode boundary): token must be the raw value.
        json_dump = config.model_dump(mode="json")
        assert json_dump["otlp_token"] == "agent-bearer-xyz", (
            f"otlp_token must be plaintext in model_dump(mode='json'); got: {json_dump['otlp_token']}"
        )
        assert json_dump["otlp_endpoint"] == "https://ingress.us-west-2.aws.dash0.com"
        assert json_dump["otlp_dataset"] == "default"
    finally:
        get_settings.cache_clear()


@pytest.mark.service
def test_build_config_update_populates_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_config_update carries Settings.environment as AgentConfig.environment,
    and it appears in the JSON-mode model_dump.
    """
    import app.core.agent_gateway.service as svc  # noqa: PLC0415
    from app.core.config import get_settings  # noqa: PLC0415

    monkeypatch.setenv("ENVIRONMENT", "staging")

    get_settings.cache_clear()
    try:
        cmd = svc._build_config_update()
        json_dump = cmd.config.model_dump(mode="json")
        assert json_dump["environment"] == "staging"
        assert cmd.config.environment == "staging"
    finally:
        get_settings.cache_clear()


@pytest.mark.service
def test_agent_config_otlp_token_redacted_in_logs() -> None:
    """str(), repr(), and model_dump() all redact otlp_token; only
    model_dump(mode='json') exposes the raw value (wire-encode boundary)."""
    from app.core.agent_gateway.types import AgentConfig  # noqa: PLC0415

    token_value = "super-secret-agent-token"
    config = AgentConfig(
        max_workspaces=2,
        otlp_endpoint="https://ingress.us-west-2.aws.dash0.com",
        otlp_token=SecretStr(token_value),
        otlp_dataset="default",
    )

    # str() and repr() must not expose the raw token.
    assert token_value not in str(config), f"Raw token leaked via str(): {config!s}"
    assert token_value not in repr(config), f"Raw token leaked via repr(): {config!r}"

    # model_dump() (Python mode) must redact.
    py_dump = config.model_dump()
    assert py_dump["otlp_token"] != token_value, f"Raw token leaked via model_dump(): {py_dump['otlp_token']}"
    # SecretStr renders as '**********' in Python mode.
    assert "**********" in str(py_dump["otlp_token"]), (
        f"Expected redaction marker in model_dump(); got: {py_dump['otlp_token']}"
    )

    # model_dump_json() (wire boundary) must expose the raw value.
    import json  # noqa: PLC0415

    wire = json.loads(config.model_dump_json())
    assert wire["otlp_token"] == token_value, (
        f"Raw token must be present in model_dump_json(); got: {wire['otlp_token']}"
    )
