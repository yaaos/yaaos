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
check_anchor_imports = _sync_modules.check_anchor_imports
check_bind_in_all = _sync_modules.check_bind_in_all
check_contextvar_in_all = _sync_modules.check_contextvar_in_all
check_dynamic_imports = _sync_modules.check_dynamic_imports
check_factory_returns_singleton = _sync_modules.check_factory_returns_singleton
check_init_business_logic = _sync_modules.check_init_business_logic
check_init_dunder_getattr = _sync_modules.check_init_dunder_getattr
check_instance_literal_in_all = _sync_modules.check_instance_literal_in_all
check_layering = _sync_modules.check_layering
check_mutable_container_in_all = _sync_modules.check_mutable_container_in_all
check_private_in_all = _sync_modules.check_private_in_all
check_private_reach = _sync_modules.check_private_reach
check_relative_imports = _sync_modules.check_relative_imports
check_star_imports = _sync_modules.check_star_imports
check_submodule_imports = _sync_modules.check_submodule_imports
check_submodule_reexports = _sync_modules.check_submodule_reexports
check_syntax_errors = _sync_modules.check_syntax_errors
check_test_helper_exports = _sync_modules.check_test_helper_exports
check_wildcard_all_expansion = _sync_modules.check_wildcard_all_expansion
discover_modules = _sync_modules.discover_modules
parse_module_interface = _sync_modules.parse_module_interface
run_tach_check = _sync_modules.run_tach_check
APP = Path(_sync_modules.APP)
BACKEND = Path(_sync_modules.BACKEND)
TACH_TOML = Path(_sync_modules.TACH_TOML)
_SYNTAX_ERROR_SENTINEL: str = _sync_modules._SYNTAX_ERROR_SENTINEL

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


# ---------------------------------------------------------------------------
# Rule-5 cross-file resolution canaries
# ---------------------------------------------------------------------------
#
# These exercise the resolver/closure that follow `from app.<m> import X`
# re-exports back to a definition site and treat locally renamed Row types
# (Assign / TypeAlias / `type` / subclass-of-Row / aliased import) as Rows.
# Synthetic backend trees under tmp_path; check_all_boundary_violations is
# called with app_root=<tmp app dir>.


def _write(path: Path, body: str) -> None:
    """Write a Python source file, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip())


def _make_synthetic_app(tmp_path: Path) -> Path:
    """Return the synthetic `app/` root with an empty layer skeleton."""
    app = tmp_path / "app"
    for layer in ("core", "domain", "plugins", "testing"):
        (app / layer).mkdir(parents=True, exist_ok=True)
    return app


def test_rule5_inline_function_still_caught(tmp_path: Path) -> None:
    """Baseline: an inline FunctionDef returning a Row is still flagged."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base):
            id: int
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.models import WidgetRow

        def get_widget() -> WidgetRow: ...

        __all__ = ["get_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("get_widget" in e and "WidgetRow" in e for e in errs), errs


def test_rule5_one_hop_reexport_is_caught(tmp_path: Path) -> None:
    """`__init__` re-exports a Row-returning function from a sibling submodule."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from app.domain.widgets.models import WidgetRow

        def insert_widget() -> WidgetRow: ...
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.repository import insert_widget

        __all__ = ["insert_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("insert_widget" in e for e in errs), errs
    assert any("repository.py" in e for e in errs), errs


def test_rule5_two_hop_reexport_is_caught(tmp_path: Path) -> None:
    """`__init__` → `service` → `repository`; the Row is two hops away."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from app.domain.widgets.models import WidgetRow

        def insert_widget() -> WidgetRow: ...
    """,
    )
    _write(
        mod / "service.py",
        """
        from app.domain.widgets.repository import insert_widget
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.service import insert_widget

        __all__ = ["insert_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("insert_widget" in e for e in errs), errs


def test_rule5_aliased_reexport_is_caught(tmp_path: Path) -> None:
    """`from .repo import insert_widget as create_widget` — name renamed at the seam."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from app.domain.widgets.models import WidgetRow

        def insert_widget() -> WidgetRow: ...
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.repository import insert_widget as create_widget

        __all__ = ["create_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("create_widget" in e for e in errs), errs


def test_rule5_local_type_alias_laundering_is_caught(tmp_path: Path) -> None:
    """`WidgetResult = WidgetRow` inside the definition file."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from app.domain.widgets.models import WidgetRow

        WidgetResult = WidgetRow

        def insert_widget() -> WidgetResult: ...
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.repository import insert_widget

        __all__ = ["insert_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("insert_widget" in e for e in errs), errs


def test_rule5_pep695_type_alias_is_caught(tmp_path: Path) -> None:
    """PEP 695: `type WidgetResult = WidgetRow` — same defense."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from app.domain.widgets.models import WidgetRow

        type WidgetResult = WidgetRow

        def insert_widget() -> WidgetResult: ...
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.repository import insert_widget

        __all__ = ["insert_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("insert_widget" in e for e in errs), errs


def test_rule5_typing_typealias_value_is_caught(tmp_path: Path) -> None:
    """`WidgetResult: TypeAlias = WidgetRow` (AnnAssign form)."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from typing import TypeAlias
        from app.domain.widgets.models import WidgetRow

        WidgetResult: TypeAlias = WidgetRow

        def insert_widget() -> WidgetResult: ...
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.repository import insert_widget

        __all__ = ["insert_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("insert_widget" in e for e in errs), errs


def test_rule5_chained_alias_is_caught(tmp_path: Path) -> None:
    """`A = WidgetRow; B = A` — chained alias closure must reach B."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from app.domain.widgets.models import WidgetRow

        WidgetRef = WidgetRow
        WidgetHandle = WidgetRef

        def insert_widget() -> WidgetHandle: ...
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.repository import insert_widget

        __all__ = ["insert_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("insert_widget" in e for e in errs), errs


def test_rule5_subscripted_alias_is_caught(tmp_path: Path) -> None:
    """`WidgetList = list[WidgetRow]` — RHS subscript still surfaces the Row."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from app.domain.widgets.models import WidgetRow

        WidgetList = list[WidgetRow]

        def list_widgets() -> WidgetList: ...
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.repository import list_widgets

        __all__ = ["list_widgets"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("list_widgets" in e for e in errs), errs


def test_rule5_subclass_of_row_is_caught(tmp_path: Path) -> None:
    """`class WidgetResult(WidgetRow)` — subclass-of-Row laundering."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from app.domain.widgets.models import WidgetRow

        class WidgetResult(WidgetRow): ...

        def insert_widget() -> WidgetResult: ...
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.repository import insert_widget

        __all__ = ["insert_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("insert_widget" in e for e in errs), errs


def test_rule5_aliased_row_import_is_caught(tmp_path: Path) -> None:
    """`from .models import WidgetRow as WidgetResult` — alias at import site."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from app.domain.widgets.models import WidgetRow as WidgetResult

        def insert_widget() -> WidgetResult: ...
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.repository import insert_widget

        __all__ = ["insert_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("insert_widget" in e for e in errs), errs


def test_rule5_clean_reexport_is_not_flagged(tmp_path: Path) -> None:
    """Re-exporting a function returning a plain dataclass must not trip Rule-5."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from dataclasses import dataclass

        @dataclass
        class WidgetView:
            id: int

        def insert_widget() -> WidgetView: ...
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.repository import insert_widget

        __all__ = ["insert_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert errs == [], errs


def test_rule5_parameter_annotation_via_reexport_is_caught(tmp_path: Path) -> None:
    """Parameter annotations also count — `def x(w: WidgetRow)` through a re-export."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "models.py",
        """
        class Base: ...
        class WidgetRow(Base): ...
    """,
    )
    _write(
        mod / "repository.py",
        """
        from app.domain.widgets.models import WidgetRow

        def touch_widget(w: WidgetRow) -> None: ...
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.repository import touch_widget

        __all__ = ["touch_widget"]
    """,
    )
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert any("touch_widget" in e for e in errs), errs


def test_rule5_cycle_is_safe(tmp_path: Path) -> None:
    """A re-export cycle must terminate (no infinite loop, no false flag)."""
    app = _make_synthetic_app(tmp_path)
    mod = app / "domain" / "widgets"
    _write(
        mod / "a.py",
        """
        from app.domain.widgets.b import insert_widget
    """,
    )
    _write(
        mod / "b.py",
        """
        from app.domain.widgets.a import insert_widget
    """,
    )
    _write(
        mod / "__init__.py",
        """
        from app.domain.widgets.a import insert_widget

        __all__ = ["insert_widget"]
    """,
    )
    # No definition anywhere — resolver returns None; rule does not crash and
    # does not emit a violation for an unresolvable chain.
    errs = check_all_boundary_violations([("domain", "widgets")], app_root=app)
    assert errs == [], errs


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
    """tach.toml is up to date with the source tree.

    ``bin/sync_modules --check`` exits 1 when tach.toml is stale; exit 2
    signals rule violations (pre-existing, tracked separately) but tach.toml
    IS in sync.  This test fails only on exit 1.
    """
    rc = _run_sync_modules()
    assert rc != 1, f"bin/sync_modules --check exited {rc}; tach.toml is stale — run bin/sync_modules"


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


# ---------------------------------------------------------------------------
# Rule-9 canary (namespace-handle in __all__).
# ---------------------------------------------------------------------------


def test_injected_namespace_handle_in_all_is_rejected() -> None:
    """Rule-9: a submodule reference in __all__ is flagged.

    Injects a fresh module under app/core with `__init__.py` containing
    `from app.core.<mod>._impl import _impl` and `__all__ = ["_impl"]` —
    where `_impl` is a sibling submodule. The classifier detects the
    namespace-handle binding and fires.
    """
    mod_dir = APP / "core" / "_rule9_canary"
    init = mod_dir / "__init__.py"
    sub = mod_dir / "_impl.py"
    mod_dir.mkdir(parents=True, exist_ok=True)
    sub.write_text("X = 1\n")
    init.write_text(
        textwrap.dedent(
            """\
            from app.core._rule9_canary import _impl  # type: ignore[unused-ignore]
            __all__ = ["_impl"]
            """
        )
    )
    # Need also a module.py so it can be picked up by discover_modules — not
    # strictly required, the discover_modules pattern is "has __init__.py".
    try:
        modules = discover_modules()
        errors = check_submodule_reexports(modules)
        assert errors, "expected Rule-9 violation but check_submodule_reexports returned none"
        assert any("_rule9_canary" in e and "_impl" in e for e in errors), (
            f"expected Rule-9 hit on _rule9_canary but got: {errors}"
        )
    finally:
        sub.unlink(missing_ok=True)
        init.unlink(missing_ok=True)
        mod_dir.rmdir()


# ---------------------------------------------------------------------------
# Rule-10 canary (ContextVar in __all__).
# ---------------------------------------------------------------------------


def test_injected_contextvar_in_all_is_rejected() -> None:
    """Rule-10: a ContextVar binding in __all__ is flagged."""
    mod_dir = APP / "core" / "_rule10_canary"
    init = mod_dir / "__init__.py"
    mod_dir.mkdir(parents=True, exist_ok=True)
    init.write_text(
        textwrap.dedent(
            """\
            from contextvars import ContextVar
            X = ContextVar("X", default=None)
            __all__ = ["X"]
            """
        )
    )
    try:
        modules = discover_modules()
        errors = check_contextvar_in_all(modules)
        assert errors, "expected Rule-10 violation but check_contextvar_in_all returned none"
        assert any("_rule10_canary" in e and '"X"' in e for e in errors), (
            f"expected Rule-10 hit on _rule10_canary but got: {errors}"
        )
    finally:
        init.unlink(missing_ok=True)
        mod_dir.rmdir()


def test_clean_tree_has_no_contextvar_in_all() -> None:
    """Clean tree: no module exports a ContextVar."""
    errors = check_contextvar_in_all(discover_modules())
    assert not errors, f"clean tree should have zero Rule-10 violations but got: {errors}"


# ---------------------------------------------------------------------------
# Rule-12 canary (instance literal in __all__).
# ---------------------------------------------------------------------------


def test_injected_instance_literal_in_all_is_rejected() -> None:
    """Rule-12: a class-instance binding in __all__ is flagged.

    Uses a fake non-data-type class (not in _DATA_TYPE_BASE_HINTS).
    """
    mod_dir = APP / "core" / "_rule12_canary"
    init = mod_dir / "__init__.py"
    mod_dir.mkdir(parents=True, exist_ok=True)
    init.write_text(
        textwrap.dedent(
            """\
            class _Registry:
                pass
            engine = _Registry()
            __all__ = ["engine"]
            """
        )
    )
    try:
        modules = discover_modules()
        errors = check_instance_literal_in_all(modules)
        assert errors, "expected Rule-12 violation but check_instance_literal_in_all returned none"
        assert any("_rule12_canary" in e and '"engine"' in e for e in errors), (
            f"expected Rule-12 hit on _rule12_canary but got: {errors}"
        )
    finally:
        init.unlink(missing_ok=True)
        mod_dir.rmdir()


def test_data_type_literal_in_all_is_allowed() -> None:
    """Rule-12: a data-type class instance (e.g. Workflow(...)) in __all__ is allowed."""
    mod_dir = APP / "core" / "_rule12_data_canary"
    init = mod_dir / "__init__.py"
    mod_dir.mkdir(parents=True, exist_ok=True)
    init.write_text(
        textwrap.dedent(
            """\
            class Workflow:
                pass
            pr_review = Workflow()
            __all__ = ["pr_review"]
            """
        )
    )
    try:
        modules = discover_modules()
        errors = check_instance_literal_in_all(modules)
        assert not any("_rule12_data_canary" in e for e in errors), (
            f"data-type instance literal wrongly flagged: {[e for e in errors if '_rule12_data_canary' in e]}"
        )
    finally:
        init.unlink(missing_ok=True)
        mod_dir.rmdir()


def test_clean_tree_has_no_instance_literal_in_all() -> None:
    """Clean tree: no module exports a non-data instance literal."""
    errors = check_instance_literal_in_all(discover_modules())
    assert not errors, f"clean tree should have zero Rule-12 violations but got: {errors}"


# ---------------------------------------------------------------------------
# Rule-15 canary (factory returns singleton in __all__).
# ---------------------------------------------------------------------------


def test_injected_factory_returns_singleton_is_rejected() -> None:
    """Rule-15: ``def get_X(): return _Y_var.get()`` in __all__ is flagged."""
    mod_dir = APP / "core" / "_rule15_canary"
    init = mod_dir / "__init__.py"
    mod_dir.mkdir(parents=True, exist_ok=True)
    init.write_text(
        textwrap.dedent(
            """\
            from contextvars import ContextVar
            _var = ContextVar("_var", default=None)
            def get_thing():
                return _var.get()
            __all__ = ["get_thing"]
            """
        )
    )
    try:
        modules = discover_modules()
        errors = check_factory_returns_singleton(modules)
        assert errors, "expected Rule-15 violation but check_factory_returns_singleton returned none"
        assert any("_rule15_canary" in e and '"get_thing"' in e for e in errors), (
            f"expected Rule-15 hit on _rule15_canary but got: {errors}"
        )
    finally:
        init.unlink(missing_ok=True)
        mod_dir.rmdir()


def test_clean_tree_has_no_factory_returns_singleton() -> None:
    """Clean tree: no module exports a factory that returns the live singleton."""
    errors = check_factory_returns_singleton(discover_modules())
    assert not errors, f"clean tree should have zero Rule-15 violations but got: {errors}"


# ---------------------------------------------------------------------------
# Rule-16 canary (mutable container in __all__).
# ---------------------------------------------------------------------------


def test_injected_mutable_container_in_all_is_rejected() -> None:
    """Rule-16: ``REGISTRY: dict = {}`` exported is flagged."""
    mod_dir = APP / "core" / "_rule16_canary"
    init = mod_dir / "__init__.py"
    mod_dir.mkdir(parents=True, exist_ok=True)
    init.write_text(
        textwrap.dedent(
            """\
            REGISTRY: dict = {}
            __all__ = ["REGISTRY"]
            """
        )
    )
    try:
        modules = discover_modules()
        errors = check_mutable_container_in_all(modules)
        assert errors, "expected Rule-16 violation but check_mutable_container_in_all returned none"
        assert any("_rule16_canary" in e and '"REGISTRY"' in e for e in errors), (
            f"expected Rule-16 hit on _rule16_canary but got: {errors}"
        )
    finally:
        init.unlink(missing_ok=True)
        mod_dir.rmdir()


def test_clean_tree_has_no_mutable_container_in_all() -> None:
    """Clean tree: no module exports a mutable container literal."""
    errors = check_mutable_container_in_all(discover_modules())
    assert not errors, f"clean tree should have zero Rule-16 violations but got: {errors}"


# ---------------------------------------------------------------------------
# Rule-17 canary (bind_* in __all__).
# ---------------------------------------------------------------------------


def test_injected_bind_in_all_is_rejected() -> None:
    """Rule-17: a ``bind_*`` name in __all__ is flagged regardless of binding."""
    mod_dir = APP / "core" / "_rule17_canary"
    init = mod_dir / "__init__.py"
    mod_dir.mkdir(parents=True, exist_ok=True)
    init.write_text(
        textwrap.dedent(
            """\
            def bind_thing(x):
                pass
            __all__ = ["bind_thing"]
            """
        )
    )
    try:
        modules = discover_modules()
        errors = check_bind_in_all(modules)
        assert errors, "expected Rule-17 violation but check_bind_in_all returned none"
        assert any("_rule17_canary" in e and '"bind_thing"' in e for e in errors), (
            f"expected Rule-17 hit on _rule17_canary but got: {errors}"
        )
    finally:
        init.unlink(missing_ok=True)
        mod_dir.rmdir()


# ---------------------------------------------------------------------------
# private-in-__all__ canary.
# ---------------------------------------------------------------------------


def test_injected_private_name_in_all_is_rejected() -> None:
    """Underscore-prefixed entries in __all__ are flagged (dunders exempt)."""
    mod_dir = APP / "core" / "_rule_private_canary"
    init = mod_dir / "__init__.py"
    mod_dir.mkdir(parents=True, exist_ok=True)
    init.write_text(
        textwrap.dedent(
            """\
            _SECRET = 1
            __all__ = ["_SECRET"]
            """
        )
    )
    try:
        modules = discover_modules()
        errors = check_private_in_all(modules)
        assert errors, "expected private-in-__all__ violation but got none"
        assert any("_rule_private_canary" in e and '"_SECRET"' in e for e in errors), (
            f"expected private-in-__all__ hit on _rule_private_canary but got: {errors}"
        )
    finally:
        init.unlink(missing_ok=True)
        mod_dir.rmdir()


# ---------------------------------------------------------------------------
# __getattr__ in __init__.py canary.
# ---------------------------------------------------------------------------


def test_injected_dunder_getattr_in_init_is_rejected() -> None:
    """``def __getattr__`` at module level in __init__.py is flagged."""
    mod_dir = APP / "core" / "_rule_getattr_canary"
    init = mod_dir / "__init__.py"
    mod_dir.mkdir(parents=True, exist_ok=True)
    init.write_text(
        textwrap.dedent(
            """\
            def __getattr__(name):
                return None
            __all__ = []
            """
        )
    )
    try:
        modules = discover_modules()
        errors = check_init_dunder_getattr(modules)
        assert errors, "expected __getattr__-in-__init__ violation but got none"
        assert any("_rule_getattr_canary" in e for e in errors), (
            f"expected __getattr__-in-__init__ hit on _rule_getattr_canary but got: {errors}"
        )
    finally:
        init.unlink(missing_ok=True)
        mod_dir.rmdir()


def test_clean_tree_has_no_dunder_getattr_in_init() -> None:
    """Clean tree: no module declares __getattr__ in __init__.py."""
    errors = check_init_dunder_getattr(discover_modules())
    assert not errors, f"clean tree should have zero __getattr__-in-__init__ violations but got: {errors}"


# ---------------------------------------------------------------------------
# Rule-18 canary (def/class in __init__.py).
# ---------------------------------------------------------------------------


def test_injected_def_in_init_is_rejected() -> None:
    """Rule-18: a top-level ``def`` in __init__.py is flagged."""
    mod_dir = APP / "core" / "_rule18_canary"
    init = mod_dir / "__init__.py"
    mod_dir.mkdir(parents=True, exist_ok=True)
    init.write_text(
        textwrap.dedent(
            """\
            def helper():
                return 1
            __all__ = ["helper"]
            """
        )
    )
    try:
        modules = discover_modules()
        errors = check_init_business_logic(modules)
        assert errors, "expected Rule-18 violation but check_init_business_logic returned none"
        assert any("_rule18_canary" in e and "helper" in e for e in errors), (
            f"expected Rule-18 hit on _rule18_canary but got: {errors}"
        )
    finally:
        init.unlink(missing_ok=True)
        mod_dir.rmdir()


# ---------------------------------------------------------------------------
# Rule-19 canary (dynamic __all__).
# ---------------------------------------------------------------------------


def test_injected_dynamic_all_is_rejected() -> None:
    """Rule-19: __all__ built by concatenation or call is flagged."""
    mod_dir = APP / "core" / "_rule19_canary"
    init = mod_dir / "__init__.py"
    mod_dir.mkdir(parents=True, exist_ok=True)
    init.write_text(
        textwrap.dedent(
            """\
            _BASE = ["foo"]
            __all__ = _BASE + ["bar"]
            """
        )
    )
    try:
        modules = discover_modules()
        errors = check_wildcard_all_expansion(modules)
        assert errors, "expected Rule-19 violation but check_wildcard_all_expansion returned none"
        assert any("_rule19_canary" in e for e in errors), (
            f"expected Rule-19 hit on _rule19_canary but got: {errors}"
        )
    finally:
        init.unlink(missing_ok=True)
        mod_dir.rmdir()


def test_clean_tree_has_no_dynamic_all() -> None:
    """Clean tree: every __all__ is a literal list/tuple of string constants."""
    errors = check_wildcard_all_expansion(discover_modules())
    assert not errors, f"clean tree should have zero Rule-19 violations but got: {errors}"


# ---------------------------------------------------------------------------
# anchor-import canary (1- and 2-segment app.* imports).
# ---------------------------------------------------------------------------


def test_injected_anchor_import_is_rejected() -> None:
    """``from app.core import X`` is flagged as an anchor import."""
    canary = APP / "core" / "audit_log" / "_anchor_canary.py"
    canary.write_text("from app.core import audit_log  # noqa\n")
    try:
        errors = check_anchor_imports(discover_modules())
        assert errors, "expected anchor-import violation but check_anchor_imports returned none"
        assert any("_anchor_canary.py" in e for e in errors), (
            f"expected anchor-import hit on _anchor_canary.py but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)


def test_injected_bare_app_import_is_rejected() -> None:
    """``import app`` (1-segment) is flagged."""
    canary = APP / "core" / "audit_log" / "_bare_app_canary.py"
    canary.write_text("import app  # noqa\n")
    try:
        errors = check_anchor_imports(discover_modules())
        assert errors, "expected anchor-import violation but check_anchor_imports returned none"
        assert any("_bare_app_canary.py" in e for e in errors), (
            f"expected anchor-import hit on _bare_app_canary.py but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# relative-import canary.
# ---------------------------------------------------------------------------


def test_injected_relative_import_is_rejected() -> None:
    """``from .foo import X`` and ``from ..bar import Y`` are flagged."""
    canary = APP / "core" / "audit_log" / "_relative_canary.py"
    canary.write_text("from . import actor  # noqa\n")
    try:
        errors = check_relative_imports(discover_modules())
        assert errors, "expected relative-import violation but check_relative_imports returned none"
        assert any("_relative_canary.py" in e for e in errors), (
            f"expected relative-import hit on _relative_canary.py but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)


def test_clean_tree_has_no_relative_imports() -> None:
    """Clean tree: every import in app/ is absolute."""
    errors = check_relative_imports(discover_modules())
    assert not errors, f"clean tree should have zero relative-import violations but got: {errors}"


# ---------------------------------------------------------------------------
# dynamic-import canary.
# ---------------------------------------------------------------------------


def test_injected_importlib_call_is_rejected() -> None:
    """``importlib.import_module(...)`` anywhere under app/ is flagged."""
    canary = APP / "core" / "audit_log" / "_dynamic_canary.py"
    canary.write_text(
        textwrap.dedent(
            """\
            import importlib
            def f():
                return importlib.import_module("os")
            """
        )
    )
    try:
        errors = check_dynamic_imports(discover_modules())
        assert errors, "expected dynamic-import violation but check_dynamic_imports returned none"
        assert any("_dynamic_canary.py" in e for e in errors), (
            f"expected dynamic-import hit on _dynamic_canary.py but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)


def test_injected_dunder_import_call_is_rejected() -> None:
    """``__import__('x')`` is flagged."""
    canary = APP / "core" / "audit_log" / "_dunder_import_canary.py"
    canary.write_text("def f():\n    return __import__('os')\n")
    try:
        errors = check_dynamic_imports(discover_modules())
        assert errors, "expected dynamic-import violation but got none"
        assert any("_dunder_import_canary.py" in e for e in errors), (
            f"expected dynamic-import hit on _dunder_import_canary.py but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# star-import canary.
# ---------------------------------------------------------------------------


def test_injected_star_import_is_rejected() -> None:
    """``from X import *`` is flagged."""
    canary = APP / "core" / "audit_log" / "_star_canary.py"
    canary.write_text("from os import *  # noqa\n")
    try:
        errors = check_star_imports(discover_modules())
        assert errors, "expected star-import violation but check_star_imports returned none"
        assert any("_star_canary.py" in e for e in errors), (
            f"expected star-import hit on _star_canary.py but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)


def test_clean_tree_has_no_star_imports() -> None:
    """Clean tree: no ``from X import *`` anywhere under app/."""
    errors = check_star_imports(discover_modules())
    assert not errors, f"clean tree should have zero star-import violations but got: {errors}"


# ---------------------------------------------------------------------------
# D3 case-sensitivity canary.
# ---------------------------------------------------------------------------


def test_case_collision_actor_is_not_flagged() -> None:
    """D3: ``from app.core.audit_log import Actor`` is NOT a Rule-6 violation.

    ``Actor`` is a class re-exported from ``actor.py``; ``actor.py`` exists as
    a sibling but the case-sensitive ``_is_submodule`` (and the AST classifier)
    distinguish the function/class re-export from a namespace handle.
    """
    canary = APP / "core" / "workflow" / "_d3_actor_canary.py"
    canary.write_text("from app.core.audit_log import Actor  # noqa\n")
    try:
        modules = discover_modules()
        errors = check_submodule_imports(modules)
        assert not any("_d3_actor_canary.py" in e for e in errors), (
            f"case-sensitive _is_submodule should not flag Actor but got: "
            f"{[e for e in errors if '_d3_actor_canary.py' in e]}"
        )
    finally:
        canary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# D5: sanctioned test-binder exemption.
# ---------------------------------------------------------------------------


def test_set_for_tests_export_is_allowed_without_production_importer() -> None:
    """``set_*_for_tests`` in ``__all__`` passes even with zero production importers.

    The D5 registry refactor sanctions this pattern: every registry module
    exposes a ``set_X_for_tests`` context manager for autouse isolation
    fixtures.  The checker must NOT flag it as a test-seam violation.
    """
    canary_dir = APP / "core" / "_canary_set_for_tests"
    canary_init = canary_dir / "__init__.py"
    canary_dir.mkdir(exist_ok=True)
    canary_init.write_text(
        "__all__ = ['set_registry_for_tests']\n"
        "\n"
        "def set_registry_for_tests() -> None:  # noqa: ANN201\n"
        "    pass\n"
    )
    try:
        modules = discover_modules()
        errors = check_test_helper_exports(modules)
        flagged = [e for e in errors if "_canary_set_for_tests" in e]
        assert not flagged, f"set_*_for_tests should be exempt from the test-seam check but got: {flagged}"
    finally:
        canary_init.unlink(missing_ok=True)
        canary_dir.rmdir()


def test_other_seam_names_still_require_production_importer() -> None:
    """Non-sanctioned test-seam names (``reset_*``) are still flagged without a production importer."""
    canary_dir = APP / "core" / "_canary_reset_seam"
    canary_init = canary_dir / "__init__.py"
    canary_dir.mkdir(exist_ok=True)
    canary_init.write_text(
        "__all__ = ['reset_my_state']\n\ndef reset_my_state() -> None:  # noqa: ANN201\n    pass\n"
    )
    try:
        modules = discover_modules()
        errors = check_test_helper_exports(modules)
        assert any("_canary_reset_seam" in e for e in errors), (
            f"reset_my_state with no production importer should be flagged but got: {errors}"
        )
    finally:
        canary_init.unlink(missing_ok=True)
        canary_dir.rmdir()


# ---------------------------------------------------------------------------
# parse_module_interface / run_tach_check hardening.
# ---------------------------------------------------------------------------


def test_parse_module_interface_fails_loud_on_syntax_error(capsys: object) -> None:
    """``parse_module_interface`` returns the sentinel and prints to stderr on SyntaxError."""
    canary_dir = APP / "core" / "_canary_syntax_err"
    canary_init = canary_dir / "__init__.py"
    canary_dir.mkdir(exist_ok=True)
    canary_init.write_text("__all__ = [\n")  # deliberate syntax error
    try:
        result = parse_module_interface("core", "_canary_syntax_err")
        captured = capsys.readouterr()  # type: ignore[union-attr]
        assert _SYNTAX_ERROR_SENTINEL in result, f"expected sentinel in result but got: {result}"
        assert "SyntaxError" in captured.err, f"expected stderr diagnostic but got: {captured.err!r}"
    finally:
        canary_init.unlink(missing_ok=True)
        canary_dir.rmdir()


def test_run_tach_check_fails_when_uv_missing(
    monkeypatch: object,
    capsys: object,
) -> None:
    """``run_tach_check`` returns exit code 2 and prints a hint when ``uv`` is missing."""
    import os  # noqa: PLC0415

    original_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", "")  # type: ignore[union-attr]
    try:
        rc = run_tach_check()
        captured = capsys.readouterr()  # type: ignore[union-attr]
        assert rc == 2, f"expected rc=2 when uv is missing but got: {rc}"
        assert "uv" in captured.err.lower(), f"expected stderr install hint but got: {captured.err!r}"
    finally:
        monkeypatch.setenv("PATH", original_path)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Rule-7: walrus (NamedExpr) and subscript receivers.
# ---------------------------------------------------------------------------


def test_injected_walrus_private_reach_is_rejected() -> None:
    """Rule-7 catches ``(eng := cross_module_call())._private_attr``."""
    canary = APP / "core" / "audit_log" / "_rule7_walrus_canary.py"
    canary.write_text(
        "from app.core.workflow import get_engine  # noqa\n"
        "\n"
        "def poke() -> None:\n"
        "    (eng := get_engine())._workflows.clear()\n"
    )
    try:
        errors = check_private_reach(discover_modules())
        assert any("_rule7_walrus_canary.py" in e for e in errors), (
            f"expected Rule-7 walrus violation but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)


def test_injected_subscript_private_reach_is_rejected() -> None:
    """Rule-7 catches ``cross_module_call()["key"]._private_attr``."""
    canary = APP / "core" / "audit_log" / "_rule7_subscript_canary.py"
    canary.write_text(
        "from app.core.workflow import get_engine  # noqa\n"
        "\n"
        "def poke() -> None:\n"
        '    get_engine()["x"]._internal = None\n'
    )
    try:
        errors = check_private_reach(discover_modules())
        assert any("_rule7_subscript_canary.py" in e for e in errors), (
            f"expected Rule-7 subscript violation but got: {errors}"
        )
    finally:
        canary.unlink(missing_ok=True)
