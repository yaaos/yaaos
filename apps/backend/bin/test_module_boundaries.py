"""Canary tests for sync_modules __all__ boundary guards (Rule-1 and Rule-5).

Each test injects a real violation into a temp module, runs the real enforcer
against it, and asserts non-zero exit. try/finally restores the original file.

Rule-1: no SQLAlchemy/mapped class in __all__.
Rule-5: no Row type in a public function's annotation.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Load sync_modules (no .py extension).
# ---------------------------------------------------------------------------

_SYNC_MODULES_PATH = Path(__file__).parent / "sync_modules"
_spec = importlib.util.spec_from_loader(
    "sync_modules",
    importlib.machinery.SourceFileLoader("sync_modules", str(_SYNC_MODULES_PATH)),
)
assert _spec is not None and _spec.loader is not None
_sync_modules: Any = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("sync_modules", _sync_modules)
_spec.loader.exec_module(_sync_modules)  # type: ignore[union-attr]

check_all_boundary_violations = _sync_modules.check_all_boundary_violations
APP = Path(_sync_modules.APP)

# The real tickets __init__.py — used as the injection target.
TICKETS_INIT = APP / "domain" / "tickets" / "__init__.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_sync_modules() -> int:
    """Run bin/sync_modules (read-only --check mode) and return its exit code."""
    result = subprocess.run(
        [sys.executable, str(_SYNC_MODULES_PATH), "--check"],
        capture_output=True,
    )
    return result.returncode


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_row_readded_to_all_is_rejected(tmp_path: Path) -> None:
    """Rule-1: re-adding TicketRow to __all__ is detected by check_all_boundary_violations."""
    original = TICKETS_INIT.read_text()
    # Inject: add "TicketRow" to __all__ and import it so the name resolves.
    poisoned = original.replace(
        "from app.domain.tickets.service import (",
        "from app.domain.tickets.models import TicketRow\nfrom app.domain.tickets.service import (",
    ).replace(
        '"InvalidTicketTransition",',
        '"InvalidTicketTransition",\n    "TicketRow",',
    )
    try:
        TICKETS_INIT.write_text(poisoned)
        errors = check_all_boundary_violations([("domain", "tickets")])
        assert errors, "expected Rule-1 violations but got none"
        assert any("TicketRow" in e for e in errors), f"expected TicketRow in errors but got: {errors}"
    finally:
        TICKETS_INIT.write_text(original)


def test_row_in_public_signature_is_rejected(tmp_path: Path) -> None:
    """Rule-5: annotating a public __all__ function with -> TicketRow fails."""
    original = TICKETS_INIT.read_text()
    # Inject: a stub public function with a Row return annotation.
    stub = textwrap.dedent("""\
        from app.domain.tickets.models import TicketRow as _TicketRow

        def get_raw_ticket() -> _TicketRow: ...

        """)
    poisoned = original.replace(
        "__all__ = [",
        stub + '__all__ = [\n    "get_raw_ticket",\n',
    )
    try:
        TICKETS_INIT.write_text(poisoned)
        errors = check_all_boundary_violations([("domain", "tickets")])
        assert errors, "expected Rule-5 violations but got none"
        assert any("get_raw_ticket" in e for e in errors), (
            f"expected get_raw_ticket in errors but got: {errors}"
        )
    finally:
        TICKETS_INIT.write_text(original)


def test_noqa_on_violation_still_fails(tmp_path: Path) -> None:
    """A violation line carrying # noqa is NOT exempted — noqa suppresses linters, not guards."""
    original = TICKETS_INIT.read_text()
    poisoned = original.replace(
        "from app.domain.tickets.service import (",
        "from app.domain.tickets.models import TicketRow  # noqa: F401\nfrom app.domain.tickets.service import (",
    ).replace(
        '"InvalidTicketTransition",',
        '"InvalidTicketTransition",\n    "TicketRow",  # noqa: F401',
    )
    try:
        TICKETS_INIT.write_text(poisoned)
        errors = check_all_boundary_violations([("domain", "tickets")])
        assert errors, "expected violations even with # noqa but got none"
        assert any("TicketRow" in e for e in errors), f"expected TicketRow in errors but got: {errors}"
    finally:
        TICKETS_INIT.write_text(original)
