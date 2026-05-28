"""The five reviewer workflows + their commands register against
`core/workflow.get_engine()` at `domain/reviewer` import.

These tests don't run the workflows end-to-end. They assert that the
registry shape lines up with the workflow definitions so a typo in a
step's `command_kind` is caught at the seam.
"""

from __future__ import annotations

import importlib

import pytest

from app.core.workflow import get_engine
from app.domain.reviewer.commands import (
    ALL_LOCAL_COMMANDS,
    ALL_WORKSPACE_COMMANDS,
)
from app.domain.reviewer.workflows import ALL_WORKFLOWS


@pytest.fixture(autouse=True)
def _ensure_registered() -> None:
    """Tests in this file run after `domain.reviewer` has been imported (the
    `web.py` import chain pulls it in). A previous test may have replaced the
    workflow engine singleton. Re-import the module to re-trigger registration."""
    import app.domain.reviewer as _r  # noqa: PLC0415

    importlib.reload(_r)
    yield


def test_all_five_workflows_registered() -> None:
    engine = get_engine()
    expected = {
        "pr_review_v1",
        "incremental_review_v1",
        "verify_fix_v1",
        "stale_check_v1",
        "answer_question_v1",
    }
    assert expected.issubset(set(engine.registered_workflow_names()))


def test_each_workflow_step_resolves_to_a_registered_command() -> None:
    """If a workflow references a `command_kind` no command registered, the
    engine's `start()` would fail at runtime. Catch that at import-time
    coherence here."""
    engine = get_engine()
    for wf in ALL_WORKFLOWS:
        for step in wf.steps:
            cmd = engine.get_command(step.command_kind)
            assert cmd is not None, (
                f"workflow {wf.name!r} step {step.id!r} references unregistered "
                f"command_kind {step.command_kind!r}"
            )


def test_lifecycle_commands_registered() -> None:
    """The three workspace-lifecycle commands ship in `core/workspace/commands.py`
    and register via the reviewer bootstrap. Verify they're present so future
    Workspace-category review commands can rely on `ProvisionWorkspace` /
    `CleanupWorkspace` / `RefreshWorkspaceAuth` being available."""
    engine = get_engine()
    for kind in ("ProvisionWorkspace", "CleanupWorkspace", "RefreshWorkspaceAuth"):
        assert engine.get_command(kind) is not None, f"{kind!r} not registered"


def test_workspace_review_commands_registered() -> None:
    engine = get_engine()
    for cmd in ALL_WORKSPACE_COMMANDS:
        assert engine.get_command(cmd.kind) is cmd


def test_local_review_commands_registered() -> None:
    engine = get_engine()
    for cmd in ALL_LOCAL_COMMANDS:
        assert engine.get_command(cmd.kind) is cmd
