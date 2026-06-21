"""Reviewer workflows + commands register against the `core/workflow` engine
at `domain/reviewer` import.

These tests assert the registry shape so a typo in a step's `command_class.kind`
is caught at the seam.
"""

from __future__ import annotations

import importlib

import pytest

from app.core.workflow import get_command, registered_workflow_names
from app.domain.reviewer.workflows import ALL_WORKFLOWS


@pytest.fixture(autouse=True)
def _ensure_registered() -> None:  # type: ignore[no-untyped-def]
    """Tests in this file run after `domain.reviewer` has been imported (the
    `web.py` import chain pulls it in). A previous test may have replaced the
    workflow engine singleton. Re-import the module to re-trigger registration."""
    import app.domain.reviewer as _r  # noqa: PLC0415

    importlib.reload(_r)
    yield


def test_pr_review_v1_workflow_registered() -> None:
    assert "pr_review_v1" in set(registered_workflow_names())


def test_no_deleted_workflows_registered() -> None:
    """Deleted workflows must not be registered."""
    deleted = {"incremental_review_v1", "verify_fix_v1", "stale_check_v1", "answer_question_v1"}
    registered = set(registered_workflow_names())
    assert not (deleted & registered), f"deleted workflows still registered: {deleted & registered}"


def test_each_workflow_step_resolves_to_a_registered_command() -> None:
    """If a workflow references a command kind that has no command registered,
    the engine's `start()` would fail at runtime. Catch that at import-time
    coherence here."""
    for wf in ALL_WORKFLOWS:
        for s in wf.steps:
            kind = s.command_class.kind
            cmd = get_command(kind)
            assert cmd is not None, (
                f"workflow {wf.name!r} step {s.step_id!r} references unregistered command_kind {kind!r}"
            )


def test_lifecycle_commands_registered() -> None:
    """The three workspace-lifecycle commands ship in `core/workspace/commands.py`
    and register via the reviewer bootstrap. Verify they're present so future
    workspace-category review commands can rely on `ProvisionWorkspace` /
    `CleanupWorkspace` / `RefreshWorkspaceAuth` being available."""
    for kind in ("ProvisionWorkspace", "CleanupWorkspace", "RefreshWorkspaceAuth"):
        assert get_command(kind) is not None, f"{kind!r} not registered"


def test_reviewer_command_kinds_registered() -> None:
    """All reviewer-defined command kinds must be present in the engine."""
    for kind in ("CheckShouldReview", "SecretsScan", "CodeReview", "PostFindings"):
        assert get_command(kind) is not None, f"reviewer command {kind!r} not registered"
