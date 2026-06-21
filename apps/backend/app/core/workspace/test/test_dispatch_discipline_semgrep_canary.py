"""Canary test: `dispatch_helper_discipline.yaml` fires on a direct
`enqueue_command(...)` call inside a reviewer-commands-shaped file.

Injects a synthetic violation into a temp file at a path matching the
semgrep rule's `paths.include` pattern, runs semgrep, and asserts non-zero
exit. Mirrors the pattern in `apps/backend/bin/test_check_table_access.py`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Absolute path to the semgrep rule under test.
_RULE = Path(__file__).parents[4] / ".semgrep" / "dispatch_helper_discipline.yaml"

_SEMGREP = shutil.which("semgrep") or "semgrep"


def _run_semgrep(*extra_args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_SEMGREP, "--config", str(_RULE), "--error", *extra_args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def test_dispatch_discipline_rule_fires_on_direct_enqueue_command(tmp_path: Path) -> None:
    """Injecting `enqueue_command(...)` into a reviewer-commands file exits non-zero."""
    # Create a synthetic file at a path matching the rule's paths.include.
    bad_file = tmp_path / "app" / "domain" / "reviewer" / "commands" / "bad_command.py"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_text(
        "from app.core.agent_gateway import enqueue_command\n"
        "enqueue_command(org_id=..., command=..., session=...)\n"
    )

    result = _run_semgrep(str(bad_file), cwd=tmp_path)
    assert result.returncode != 0, (
        f"Expected semgrep to exit non-zero on dispatch primitive call.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_dispatch_discipline_rule_does_not_fire_on_clean_file(tmp_path: Path) -> None:
    """A reviewer-commands file with no dispatch primitives exits zero."""
    clean_file = tmp_path / "app" / "domain" / "reviewer" / "commands" / "clean_command.py"
    clean_file.parent.mkdir(parents=True, exist_ok=True)
    clean_file.write_text(
        "from app.core.workspace import dispatch_via_workspace\n"
        "# uses Layer 2 correctly — no raw dispatch primitives\n"
    )

    result = _run_semgrep(str(clean_file), cwd=tmp_path)
    assert result.returncode == 0, (
        f"Expected semgrep to exit zero on clean file.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
