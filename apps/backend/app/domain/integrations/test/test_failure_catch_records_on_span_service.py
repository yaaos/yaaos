"""Service test: failure-shaped catches in domain/integrations record exception events on spans.

Samples run_health_check_once with a validate() that raises — asserts the
span wrapping the call carries an `exception` event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import SecretStr

from app.core.oauth import ProviderConfig
from app.core.secrets import encrypt
from app.domain.integrations.models import McpCredentialRow
from app.domain.integrations.scheduler import run_health_check_once
from app.domain.integrations.types import _REGISTRY
from app.domain.orgs import repository as orgs_repo
from app.testing.observability import span_capture

pytestmark = pytest.mark.service


def _config() -> ProviderConfig:
    return ProviderConfig(
        authorize_url="https://stub.test/authorize",
        token_url="https://stub.test/token",
        refresh_url="https://stub.test/token",
        mcp_url="https://stub.test/mcp",
        client_id="cid",
        client_secret=SecretStr("csecret"),
        scope_separator=" ",
        default_scopes=("read",),
        known_read_tools=("get",),
        known_write_tools=("update",),
    )


@dataclass
class _RaisingProvider:
    provider_id: str = "stub_raising"
    config: ProviderConfig = field(default_factory=_config)

    async def validate(self, access_token: SecretStr) -> bool:
        raise RuntimeError("simulated validate failure")


@pytest.mark.asyncio
async def test_integrations_failure_catch_records_on_span(db_session) -> None:  # type: ignore[no-untyped-def]
    """validate_crashed path records exception event + ERROR on the active span."""
    prov = _RaisingProvider()
    _REGISTRY["stub_raising"] = prov
    try:
        org = await orgs_repo.insert_org(db_session, slug="integrations-span-test-org")
        row = McpCredentialRow(
            org_id=org.org_id,
            provider="stub_raising",
            enabled=True,
            encrypted_access_token=encrypt(b"tok").decode(),
            encrypted_refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db_session.add(row)
        await db_session.commit()

        with span_capture() as exporter:
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("spawn:integrations.scheduler"):
                await run_health_check_once()
    finally:
        _REGISTRY.pop("stub_raising", None)

    spans = exporter.get_finished_spans()
    target = next(
        (s for s in spans if "integrations.scheduler" in s.name),
        None,
    )
    assert target is not None, f"no integrations.scheduler span; got: {[s.name for s in spans]}"

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, f"expected exception event on span, got: {[e.name for e in target.events]}"
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status, got {target.status.status_code}"
    )
