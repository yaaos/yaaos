"""Tests for the stub workspace wrapper."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.core.plugin_kit import PluginMeta
from app.core.workspace import (
    CodingAgentCliResult,
    HealthStatus,
    RepoRefForSpec,
    WorkspaceSpec,
    get_provider,
    register_workspace_provider,
)
from app.testing.stub_workspace import (
    StubWorkspaceProvider,
    wrap_all_registered_workspace_providers,
)


class _FakeProvider:
    """Minimal WorkspaceProvider for stub-wrapping tests. Returns a real tempdir
    from provision() so the stub's 'no clone' path is exercisable end-to-end."""

    meta = PluginMeta(id="fake_ws", type="workspace", display_name="Fake WS")

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        import tempfile  # noqa: PLC0415

        working_dir = tempfile.mkdtemp(prefix="yaaos-fake-ws-")
        return {"working_dir": working_dir}

    async def run_coding_agent_cli(self, plugin_state, argv, **kwargs):  # type: ignore[no-untyped-def]
        del plugin_state, argv, kwargs
        return CodingAgentCliResult(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=0)

    async def read_text(self, plugin_state, path):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, plugin_state, path, content):  # type: ignore[no-untyped-def]
        return None

    async def destroy(self, plugin_state):  # type: ignore[no-untyped-def]
        import shutil  # noqa: PLC0415

        if working_dir := plugin_state.get("working_dir"):
            shutil.rmtree(working_dir, ignore_errors=True)

    async def health_check(self):  # type: ignore[no-untyped-def]
        return HealthStatus(healthy=True, message="ok")


@pytest.fixture(autouse=True)
def _reset_registry(workspace_providers_isolation) -> None:
    del workspace_providers_isolation  # fixture handles clear before+after


@pytest.mark.asyncio
async def test_wrap_all_swaps_registered_providers() -> None:
    real = _FakeProvider()
    register_workspace_provider(real)
    assert get_provider("fake_ws") is real

    count = wrap_all_registered_workspace_providers()
    assert count == 1
    wrapped = get_provider("fake_ws")
    assert isinstance(wrapped, StubWorkspaceProvider)
    # meta is preserved end-to-end.
    assert wrapped.meta.id == "fake_ws"
    assert wrapped.meta.display_name == "Fake WS"


@pytest.mark.asyncio
async def test_wrap_all_is_idempotent() -> None:
    register_workspace_provider(_FakeProvider())
    wrap_all_registered_workspace_providers()
    second_call = wrap_all_registered_workspace_providers()
    assert second_call == 0  # nothing new to wrap


@pytest.mark.asyncio
async def test_stub_provision_creates_empty_tempdir() -> None:
    stub = StubWorkspaceProvider(wrapped=_FakeProvider())
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
    stub = StubWorkspaceProvider(wrapped=_FakeProvider())
    result = await stub.run_coding_agent_cli(
        {"working_dir": "/tmp/whatever"},
        argv=["does", "not", "matter"],
    )
    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.timed_out is False
