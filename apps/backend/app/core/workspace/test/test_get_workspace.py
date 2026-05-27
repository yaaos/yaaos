"""`get_workspace(workspace_id)` — resolver substrate for Workspace
WorkflowCommand bodies (CodeReview, IncrementalReview, VerifyFix, etc.)
that take a `workspace_id` input and need a live Workspace handle to
invoke `run_coding_agent_cli` against."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.plugin_kit import PluginMeta
from app.core.workspace import (
    clear_workspace_providers,
    get_workspace,
    register_workspace_provider,
)
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import WorkspaceStatus


class _StubWorkspaceProvider:
    meta = PluginMeta(id="in_process", type="workspace", display_name="stub")

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"sha": spec.sha}

    async def destroy(self, plugin_state):  # type: ignore[no-untyped-def]
        return None

    async def health_check(self, plugin_state):  # type: ignore[no-untyped-def]
        del plugin_state
        return None

    async def run_coding_agent_cli(self, plugin_state, argv, **kwargs):  # type: ignore[no-untyped-def]
        return {"plugin_state": plugin_state, "argv": argv}

    async def read_text(self, plugin_state, path):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, plugin_state, path, content):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture
def _stub_provider():
    clear_workspace_providers()
    register_workspace_provider(_StubWorkspaceProvider())
    yield
    clear_workspace_providers()


async def test_returns_none_for_missing_row(_stub_provider) -> None:
    ws = await get_workspace(uuid4())
    assert ws is None


async def test_returns_none_when_plugin_state_unset(db_session, _stub_provider) -> None:  # type: ignore[no-untyped-def]
    """Workspace that failed to provision has no plugin_state — there's no
    live handle to hand out."""
    ws_id = uuid4()
    db_session.add(
        WorkspaceRow(
            id=ws_id,
            org_id=uuid4(),
            provider_id="in_process",
            spec={"sha": "x"},
            status=WorkspaceStatus.CREATING.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            plugin_state=None,
        )
    )
    await db_session.commit()
    ws = await get_workspace(ws_id)
    assert ws is None


async def test_returns_none_when_provider_not_registered(db_session) -> None:  # type: ignore[no-untyped-def]
    """Row points to a provider id that's not currently registered —
    deployment-level misconfig. Caller surfaces as failure."""
    clear_workspace_providers()
    ws_id = uuid4()
    db_session.add(
        WorkspaceRow(
            id=ws_id,
            org_id=uuid4(),
            provider_id="in_process",
            spec={"sha": "x"},
            status=WorkspaceStatus.ACTIVE.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            plugin_state={"sha": "x"},
        )
    )
    await db_session.commit()
    ws = await get_workspace(ws_id)
    assert ws is None


async def test_returns_live_handle(db_session, _stub_provider) -> None:  # type: ignore[no-untyped-def]
    ws_id = uuid4()
    db_session.add(
        WorkspaceRow(
            id=ws_id,
            org_id=uuid4(),
            provider_id="in_process",
            spec={"sha": "deadbeef"},
            status=WorkspaceStatus.ACTIVE.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            plugin_state={"sha": "deadbeef", "working_dir": "/tmp/x"},
        )
    )
    await db_session.commit()
    ws = await get_workspace(ws_id)
    assert ws is not None
    assert ws.id == str(ws_id)
    # Delegation works: the handle forwards to the provider.
    result = await ws.run_coding_agent_cli(["echo"])
    assert result["plugin_state"]["sha"] == "deadbeef"
    assert result["argv"] == ["echo"]
