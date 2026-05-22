"""Shared setup for service tests.

`ensure_plugins_registered()` defensively re-bootstraps the plugin registries
(`coding_agent`, `workspace`, `vcs`) — some tests in the suite call
`_reset_plugins_for_tests()` which clears these registries; service tests
that drive `reviewer.start_pr_review` or `intake.handle_vcs_events` need the
real plugin entries (wrapped by `stub_coding_agent` + `stub_workspace`)
present regardless of test ordering.

Use as an autouse fixture in service-test directory `conftest.py`s.
"""

from __future__ import annotations

import os


def ensure_plugins_registered() -> None:
    """Repopulate the plugin registries if any are empty + re-wrap stubs.

    Idempotent. Cheap when registries are already populated.
    """
    from app.core.workspace.service import _PROVIDERS as _WS  # noqa: PLC0415
    from app.domain.coding_agent.service import _PLUGINS as _CA  # noqa: PLC0415
    from app.domain.vcs.registry import _PLUGINS as _VCS  # noqa: PLC0415

    if "claude_code" not in _CA:
        from app.plugins.claude_code.service import bootstrap as _cc  # noqa: PLC0415

        _cc()
    if "github" not in _VCS:
        from app.plugins.github.service import bootstrap as _gh  # noqa: PLC0415

        _gh()
    if "in_process" not in _WS:
        from app.plugins.in_memory_workspace.service import bootstrap as _ws  # noqa: PLC0415

        _ws()

    # Re-wrap the stubs if env says so. wrap_all_* are idempotent.
    if os.environ.get("YAAOS_CODING_AGENT_STUB", "").lower() in {"1", "true", "yes"}:
        from app.testing.stub_coding_agent import wrap_all_registered_plugins  # noqa: PLC0415
        from app.testing.stub_workspace import wrap_all_registered_workspace_providers  # noqa: PLC0415

        wrap_all_registered_plugins()
        wrap_all_registered_workspace_providers()


__all__ = ["ensure_plugins_registered"]
