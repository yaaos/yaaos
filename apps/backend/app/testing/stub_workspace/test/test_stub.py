"""Tests for the stub workspace wrapper."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.core.workspace import (
    RepoRefForSpec,
    WorkspaceSpec,
    register_workspace_provider,
)
from app.core.workspace.service import _PROVIDERS, clear_workspace_providers
from app.plugins.in_memory_workspace import get_provider as get_in_process_provider
from app.testing.stub_workspace import (
    StubWorkspaceProvider,
    wrap_all_registered_workspace_providers,
)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    clear_workspace_providers()
    yield
    clear_workspace_providers()


@pytest.mark.asyncio
async def test_wrap_all_swaps_registered_providers() -> None:
    real = get_in_process_provider()
    register_workspace_provider(real)
    assert _PROVIDERS["in_process"] is real

    count = wrap_all_registered_workspace_providers()
    assert count == 1
    assert isinstance(_PROVIDERS["in_process"], StubWorkspaceProvider)
    # meta is preserved end-to-end.
    assert _PROVIDERS["in_process"].meta.id == "in_process"
    assert _PROVIDERS["in_process"].meta.display_name == "In-Process Workspace"


@pytest.mark.asyncio
async def test_wrap_all_is_idempotent() -> None:
    register_workspace_provider(get_in_process_provider())
    wrap_all_registered_workspace_providers()
    second_call = wrap_all_registered_workspace_providers()
    assert second_call == 0  # nothing new to wrap


@pytest.mark.asyncio
async def test_stub_provision_creates_empty_tempdir() -> None:
    stub = StubWorkspaceProvider(wrapped=get_in_process_provider())
    state = await stub.provision(
        WorkspaceSpec(
            repo=RepoRefForSpec(plugin_id="github", external_id="acme/web"),
            sha="abc123",
            org_id=uuid4(),
        )
    )
    try:
        working_dir = state["working_dir"]
        assert os.path.isdir(working_dir)
        # Marker present, no .git directory (stub skips clone).
        assert os.path.isfile(os.path.join(working_dir, ".yaaos-workspace"))
        assert not os.path.isdir(os.path.join(working_dir, ".git"))
    finally:
        await stub.destroy(state)


@pytest.mark.asyncio
async def test_stub_run_coding_agent_cli_is_noop() -> None:
    stub = StubWorkspaceProvider(wrapped=get_in_process_provider())
    result = await stub.run_coding_agent_cli(
        {"working_dir": "/tmp/whatever"},
        argv=["does", "not", "matter"],
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.timed_out is False
