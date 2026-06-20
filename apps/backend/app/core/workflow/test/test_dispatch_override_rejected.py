"""Unit tests: `__init_subclass__` guard rejects dispatch overrides.

Both `WorkspaceOpCommand` and `CodingAgentCommand` carry `@final dispatch`
backed by an `__init_subclass__` guard that raises `TypeError` at class-body
execution time — before any instance is created — when a subclass attempts to
define `dispatch`.

These tests confirm the guard fires and that implementing the correct abstract
method (`build_command` / `build_invocation`) is accepted.
"""

from __future__ import annotations

import pytest

# ── WorkspaceOpCommand guard ────────────────────────────────────────────────


def test_workspace_op_command_dispatch_override_raises_type_error() -> None:
    """Subclassing WorkspaceOpCommand and defining `dispatch` raises TypeError."""
    from app.core.workspace import WorkspaceOpCommand  # noqa: PLC0415

    with pytest.raises(TypeError, match="cannot override @final dispatch"):

        class _Bad(WorkspaceOpCommand):
            kind = "BadWsDispatch"

            async def build_command(self, inputs, ctx, *, session):  # type: ignore[override]
                return None

            async def dispatch(self, inputs, ctx, *, session) -> None:  # type: ignore[override]
                pass


def test_workspace_op_command_build_command_override_accepted() -> None:
    """A correct WorkspaceOpCommand subclass implementing only build_command is accepted."""
    from pydantic import BaseModel  # noqa: PLC0415

    from app.core.workspace import WorkspaceOpCommand  # noqa: PLC0415

    class _GoodInputs(BaseModel):
        pass

    class _Good(WorkspaceOpCommand):
        kind = "GoodWsCmd"
        Inputs = _GoodInputs

        async def build_command(self, inputs, ctx, *, session):  # type: ignore[override]
            return None

    # No exception — class body completed without error.
    assert _Good.kind == "GoodWsCmd"


# ── CodingAgentCommand guard ────────────────────────────────────────────────


def test_coding_agent_command_dispatch_override_raises_type_error() -> None:
    """Subclassing CodingAgentCommand and defining `dispatch` raises TypeError."""
    from app.core.coding_agent import CodingAgentCommand  # noqa: PLC0415

    with pytest.raises(TypeError, match="cannot override @final dispatch"):

        class _Bad(CodingAgentCommand):
            kind = "BadCaDispatch"
            plugin_id = "claude_code"

            async def build_invocation(self, inputs, ctx, *, session):  # type: ignore[override]
                ...

            async def dispatch(self, inputs, ctx, *, session) -> None:  # type: ignore[override]
                pass


def test_coding_agent_command_build_invocation_override_accepted() -> None:
    """A correct CodingAgentCommand subclass implementing only build_invocation is accepted."""
    from pydantic import BaseModel  # noqa: PLC0415

    from app.core.coding_agent import CodingAgentCommand  # noqa: PLC0415

    class _GoodInputs(BaseModel):
        workspace_id: str

    class _Good(CodingAgentCommand):
        kind = "GoodCaCmd"
        plugin_id = "claude_code"
        Inputs = _GoodInputs

        async def build_invocation(self, inputs, ctx, *, session):  # type: ignore[override]
            raise NotImplementedError

    assert _Good.kind == "GoodCaCmd"
    assert _Good.plugin_id == "claude_code"
