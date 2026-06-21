"""Canary tests for sync_modules boundary guards.

Each test injects a real violation into a temp file or module, runs the real
enforcer against it, and asserts non-zero exit. try/finally restores originals.

Rule-1: no SQLAlchemy/mapped class in __all__.
Rule-5: no Row type in a public function's annotation.
Cycle guard: tach forbid_circular_dependencies rejects import cycles.
Layer guard: check_layering rejects core→domain edges.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import re
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
check_layering = _sync_modules.check_layering
check_private_reach = _sync_modules.check_private_reach
check_submodule_imports = _sync_modules.check_submodule_imports
check_test_helper_exports = _sync_modules.check_test_helper_exports
discover_modules = _sync_modules.discover_modules
APP = Path(_sync_modules.APP)
BACKEND = Path(_sync_modules.BACKEND)
TACH_TOML = Path(_sync_modules.TACH_TOML)

# The real tickets __init__.py — used as the injection target.
TICKETS_INIT = APP / "domain" / "tickets" / "__init__.py"

# audit_log __init__.py — used as the core-module injection target.
AUDIT_LOG_INIT = APP / "core" / "audit_log" / "__init__.py"

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


def _run_sync_modules_write() -> subprocess.CompletedProcess[bytes]:
    """Run bin/sync_modules (write mode) and return the completed process."""
    return subprocess.run(
        [sys.executable, str(_SYNC_MODULES_PATH)],
        capture_output=True,
        cwd=str(BACKEND),
    )


def _run_tach_check_interfaces() -> subprocess.CompletedProcess[bytes]:
    """Run `tach check --interfaces` and return the completed process."""
    return subprocess.run(
        ["uv", "run", "tach", "check", "--interfaces"],
        capture_output=True,
        cwd=str(BACKEND),
    )


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


# ---------------------------------------------------------------------------
# Cycle guard canaries (Layer A)
# ---------------------------------------------------------------------------


def test_tach_toml_carries_cycle_guard() -> None:
    """tach.toml must contain forbid_circular_dependencies = true (no layers/layer= tags)."""
    content = TACH_TOML.read_text()
    assert "forbid_circular_dependencies = true" in content, (
        "tach.toml is missing forbid_circular_dependencies = true; run bin/sync_modules to regenerate"
    )
    assert "layers" not in content, (
        "tach.toml must not contain tach-native layers= entries "
        "(layering is enforced by check_layering in bin/sync_modules)"
    )


def test_tach_toml_not_stale() -> None:
    """bin/sync_modules --check exits 0 — tach.toml is up to date with the source tree."""
    rc = _run_sync_modules()
    assert rc == 0, f"bin/sync_modules --check exited {rc}; tach.toml is stale — run bin/sync_modules"


def test_baseline_clean_tree_passes() -> None:
    """Both tach check --interfaces and check_layering are clean on the real tree."""
    proc = _run_tach_check_interfaces()
    assert proc.returncode == 0, (
        f"tach check --interfaces failed on clean tree (exit {proc.returncode}):\n"
        f"{proc.stdout.decode()}\n{proc.stderr.decode()}"
    )

    modules = discover_modules()
    errs = check_layering(modules)
    assert not errs, "check_layering found violations on clean tree:\n" + "\n".join(errs)


def test_injected_cycle_is_rejected() -> None:
    """A canary file that creates a cycle causes tach check --interfaces to exit non-zero.

    The tach.toml must be regenerated (write mode) after injecting the file so the
    new depends_on edge is present before tach check runs.  Teardown restores both
    the canary file and the tach.toml.
    """
    # audit_log depends on database; auth depends on audit_log.
    # Importing auth from inside audit_log creates: audit_log → auth → audit_log.
    canary = APP / "core" / "audit_log" / "_canary.py"
    original_toml = TACH_TOML.read_text()
    canary.write_text("from app.core.auth import Role\n")
    try:
        # Regenerate tach.toml so the injected import is reflected in depends_on.
        _run_sync_modules_write()
        proc = _run_tach_check_interfaces()
        assert proc.returncode != 0, (
            "tach check --interfaces should have rejected the injected cycle but exited 0"
        )
        output = proc.stdout.decode() + proc.stderr.decode()
        assert "Circular dependency" in output or "circular" in output.lower(), (
            f"expected 'Circular dependency' in tach output but got:\n{output}"
        )
    finally:
        canary.unlink(missing_ok=True)
        # Restore the original tach.toml so subsequent tests see the clean tree.
        TACH_TOML.write_text(original_toml)


# ---------------------------------------------------------------------------
# Layer guard canary (Layer B)
# ---------------------------------------------------------------------------


def test_test_helper_export_is_rejected(tmp_path: Path) -> None:
    """CI guard: a test-seam name with zero production importers in __all__ is rejected.

    Injects a ``reset_something`` function (no callers outside tests/testing) into
    the audit_log module's ``__init__.py``, asserts ``check_test_helper_exports`` flags
    it, and restores the file in ``finally``.
    """
    original = AUDIT_LOG_INIT.read_text()
    # Inject a seam-named function with no production importers.
    stub = '\n\ndef reset_something_for_ci_canary() -> None:\n    """Test-seam: no production caller."""\n\n'
    poisoned = original + stub
    # Add the name to __all__ so the checker can find it.
    if "__all__" in poisoned:
        # Insert the name right after the opening bracket of __all__ = [...].
        poisoned = re.sub(
            r"(__all__\s*=\s*\[)",
            r'\1\n    "reset_something_for_ci_canary",',
            poisoned,
            count=1,
        )
    try:
        AUDIT_LOG_INIT.write_text(poisoned)
        errors = check_test_helper_exports([("core", "audit_log")])
        assert errors, "expected test-seam violation but check_test_helper_exports returned none"
        assert any("reset_something_for_ci_canary" in e for e in errors), (
            f"expected reset_something_for_ci_canary in errors but got: {errors}"
        )
    finally:
        AUDIT_LOG_INIT.write_text(original)


def test_clean_tree_has_no_test_helper_exports() -> None:
    """check_test_helper_exports finds ZERO real violations on the clean tree."""
    modules = discover_modules()
    errs = check_test_helper_exports(modules)
    assert not errs, "check_test_helper_exports found violations on the clean tree:\n" + "\n".join(errs)


# ---------------------------------------------------------------------------
# Rule-6 / Rule-7 canaries.
# ---------------------------------------------------------------------------


def test_injected_submodule_import_is_rejected() -> None:
    """Rule-6: ``import app.<other_mod>.<sub>`` from another module is flagged.

    Injects a canary file into ``app/core/audit_log/`` that does the exact reach
    PR #67 (arch-004) flagged — ``import app.core.workflow.service as _wf``.
    Restores the canary in ``finally``.
    """
    canary = APP / "core" / "audit_log" / "_rule6_canary.py"
    canary.write_text("import app.core.workflow.service as _wf  # noqa\n")
    try:
        modules = discover_modules()
        errors = check_submodule_imports(modules)
        assert errors, "expected Rule-6 violation but check_submodule_imports returned none"
        assert any("_rule6_canary.py" in e and "app.core.workflow.service" in e for e in errors), (
            f"expected Rule-6 hit on _rule6_canary.py but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)


def test_injected_submodule_from_import_is_rejected() -> None:
    """Rule-6: ``from app.<other_mod> import <submodule>`` is flagged.

    Distinct from a symbol import: the imported name resolves to a submodule
    file/dir, not an entry in the target module's ``__all__``. This is the
    side-effect-import shape composition roots use; outside the carve-out it's
    a private-state reach.
    """
    canary = APP / "core" / "audit_log" / "_rule6_from_canary.py"
    # workflow has a `service` submodule but `service` is not in its __all__.
    canary.write_text("from app.core.workflow import service  # noqa\n")
    try:
        modules = discover_modules()
        errors = check_submodule_imports(modules)
        assert errors, "expected Rule-6 violation but check_submodule_imports returned none"
        assert any("_rule6_from_canary.py" in e and "service" in e for e in errors), (
            f"expected Rule-6 hit on _rule6_from_canary.py but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)


def test_intra_module_submodule_import_is_allowed() -> None:
    """Rule-6: a module's own files may import its own submodules.

    Inside ``app/core/audit_log/`` a file may freely ``from app.core.audit_log.X
    import Y`` — only cross-module submodule reach is forbidden.
    """
    canary = APP / "core" / "audit_log" / "_rule6_intra_canary.py"
    canary.write_text("from app.core.audit_log import models  # noqa\n")
    try:
        modules = discover_modules()
        errors = check_submodule_imports(modules)
        assert not any("_rule6_intra_canary.py" in e for e in errors), (
            f"intra-module submodule import wrongly flagged: "
            f"{[e for e in errors if '_rule6_intra_canary.py' in e]}"
        )
    finally:
        canary.unlink(missing_ok=True)


def test_injected_private_attr_via_alias_is_rejected() -> None:
    """Rule-7: ``<cross_mod_alias>._private`` write is flagged.

    Mirrors ``_wf_svc._engine = fresh`` in
    ``app/testing/workflow_harness.py`` — the exact reach PR #67 flagged.
    """
    canary = APP / "core" / "audit_log" / "_rule7_alias_canary.py"
    canary.write_text("import app.core.workflow.service as _wf  # noqa\n_wf._engine = None  # noqa\n")
    try:
        modules = discover_modules()
        errors = check_private_reach(modules)
        assert errors, "expected Rule-7 violation but check_private_reach returned none"
        assert any("_rule7_alias_canary.py" in e and "_engine" in e for e in errors), (
            f"expected Rule-7 hit on _rule7_alias_canary.py but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)


def test_injected_private_attr_via_return_taint_is_rejected() -> None:
    """Rule-7: ``engine._workflows`` flagged when ``engine`` was returned from
    a cross-module call.

    Mirrors the second reach in ``app/testing/workflow_harness.py``:
    ``engine = get_engine(); engine._workflows.pop(...)``.
    """
    canary = APP / "core" / "audit_log" / "_rule7_taint_canary.py"
    canary.write_text(
        "from app.core.workflow import get_engine  # noqa\n"
        "\n"
        "def poke() -> None:\n"
        "    engine = get_engine()\n"
        '    engine._workflows.pop("x", None)\n'
    )
    try:
        modules = discover_modules()
        errors = check_private_reach(modules)
        assert errors, "expected Rule-7 violation but check_private_reach returned none"
        assert any("_rule7_taint_canary.py" in e and "_workflows" in e for e in errors), (
            f"expected Rule-7 hit on _rule7_taint_canary.py but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)


def test_dunder_attr_on_cross_module_receiver_is_allowed() -> None:
    """Rule-7: dunder access (``__init__`` etc.) is NOT a private reach.

    Dunders are Python protocol, not module-private state. The visitor must
    skip them.
    """
    canary = APP / "core" / "audit_log" / "_rule7_dunder_canary.py"
    canary.write_text("import app.core.workflow.service as _wf  # noqa\n_name = _wf.__name__  # noqa\n")
    try:
        modules = discover_modules()
        errors = check_private_reach(modules)
        assert not any("_rule7_dunder_canary.py" in e for e in errors), (
            f"dunder access wrongly flagged: {[e for e in errors if '_rule7_dunder_canary.py' in e]}"
        )
    finally:
        canary.unlink(missing_ok=True)


def test_composition_root_submodule_imports_are_allowed() -> None:
    """Rule-6 carve-out: ``app/web.py`` and ``app/worker.py`` may side-effect-
    import submodules to wire bootstrap (route registration, shutdown hooks).

    Asserts the real, unmodified composition roots produce no Rule-6 violations
    against themselves.
    """
    modules = discover_modules()
    errors = check_submodule_imports(modules)
    bad = [e for e in errors if "app/web.py" in e or "app/worker.py" in e]
    assert not bad, f"composition root carve-out leaked Rule-6 violations: {bad}"


def test_injected_core_to_domain_is_rejected() -> None:
    """A core→domain import is caught by check_layering (tach --interfaces does NOT enforce layers)."""
    # Inject a domain import into a real core module's __init__.py.
    original = AUDIT_LOG_INIT.read_text()
    poisoned = original + "\nfrom app.domain.tickets import get  # canary\n"
    try:
        AUDIT_LOG_INIT.write_text(poisoned)
        # Regenerate tach.toml so depends_on reflects the injected import.
        _run_sync_modules_write()
        # check_layering must report the violation.
        modules = discover_modules()
        errs = check_layering(modules)
        assert errs, "expected layering violation but check_layering returned no errors"
        assert any("audit_log" in e and "domain" in e for e in errs), (
            "expected a core/audit_log → domain violation but got:\n" + "\n".join(errs)
        )
        # sync_modules main() (write mode) must also exit non-zero.
        proc = _run_sync_modules_write()
        assert proc.returncode != 0, (
            "bin/sync_modules should have exited non-zero for layering violation but exited 0"
        )
        output = proc.stdout.decode() + proc.stderr.decode()
        assert "layering" in output.lower(), f"expected 'layering' in sync_modules output but got:\n{output}"
    finally:
        AUDIT_LOG_INIT.write_text(original)
        # Restore tach.toml to match the clean tree.
        _run_sync_modules_write()
