"""Runtime import audit — catches cross-module submodule reaches at test time.

Static analysis in `bin/sync_modules` checks what source code says. This
runtime guard checks what the program does — closing dynamic-Python bypasses
static analysis can't see: ``importlib.import_module(...)``, ``__import__``,
``getattr``-triggered lazy submodule loads, plugin registries built from
strings.

Installed by ``apps/backend/conftest.py`` at the front of ``sys.meta_path``
BEFORE any ``app.*`` import. Never intercepts imports — ``find_spec`` always
returns ``None``, letting Python's normal finders resolve the module. Its
only job is to RECORD every attempted import and flag those that violate
Rule-6: a file in module A reaching into a submodule of module B (A ≠ B,
depth > 0).

After pytest finishes, ``conftest.pytest_sessionfinish`` calls
``flush_and_report()`` which writes a formatted report to stderr, dumps
machine-readable violations to ``tmp/import_audit_violations.json``, and
returns the violation count. The conftest hook then overrides pytest's exit
status to 2 on any violation.

``bin/ci`` independently asserts that ``tmp/import_audit_ran`` exists after
pytest — proving the guard installed successfully. If conftest is bypassed
or the finder is uninstalled, this sentinel check fails the build.

NO EXEMPTIONS. Loose files under ``app/`` (``app/web.py``, ``app/worker.py``,
``app/testing/seed.py``, ``app/testing/isolation.py``, etc.) are not owned by
any discovered module — when they reach into a submodule of some other
module, that is a Rule-6-class violation and surfaces. If a legitimate boot
or test-infra sequence breaks, fix the sequence; don't carve a hole in the
audit.
"""

from __future__ import annotations

import json
import sys
from importlib.abc import MetaPathFinder
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
TMP = BACKEND / "tmp"
SENTINEL = TMP / "import_audit_ran"
DUMP = TMP / "import_audit_violations.json"

LAYERS = ("core", "domain", "plugins", "testing")


# Module-scope state. Persists for the lifetime of the pytest process.
# `_violations` carries `(importer_rel, target_fullname, traceback_block)`.
# `_seen_keys` dedupes on `(importer_rel, target_fullname)` so a reach that
# fires in a loop is one violation, not many.
_violations: set[tuple[str, str, str]] = set()
_seen_keys: set[tuple[str, str]] = set()

# Lazy module discovery — computed on first find_spec invocation, cached for
# the process lifetime. Tests don't add/remove backend modules at runtime.
_discovered: frozenset[tuple[str, str]] | None = None


def _discover_modules() -> frozenset[tuple[str, str]]:
    """Return {(layer, name)} for every backend module.

    Mirrors ``bin/sync_modules.discover_modules`` — a child directory of
    ``app/<layer>/`` with an ``__init__.py`` is a discovered module.
    """
    found: set[tuple[str, str]] = set()
    for layer in LAYERS:
        layer_dir = APP / layer
        if not layer_dir.is_dir():
            continue
        for child in layer_dir.iterdir():
            if child.is_dir() and (child / "__init__.py").exists():
                found.add((layer, child.name))
    return frozenset(found)


def _resolve_module_target(fullname: str) -> tuple[str, str, int] | None:
    """Resolve a dotted import name to ``(layer, mod_name, depth)``, or None.

    ``depth`` counts segments BEYOND the module's package root.
    ``depth == 0`` means the import targets the module root; ``depth > 0``
    means it reaches into a submodule. Returns ``None`` when ``fullname``
    doesn't resolve to any discovered backend module.
    """
    global _discovered
    if _discovered is None:
        _discovered = _discover_modules()
    if not fullname.startswith("app."):
        return None
    parts = fullname.split(".")
    if len(parts) < 3 or parts[1] not in LAYERS:
        return None
    key = (parts[1], parts[2])
    if key not in _discovered:
        return None
    return (parts[1], parts[2], len(parts) - 3)


def _owning_module_for_path(path_str: str) -> tuple[str, str] | None:
    """Return ``(layer, name)`` for the file's owning module, or None.

    Loose files (``app/web.py``, ``app/worker.py``, files directly under a
    layer like ``app/testing/seed.py``) and files outside ``app/`` return
    None — they have no owning module and any cross-module submodule reach
    they make surfaces as a violation.
    """
    try:
        rel = Path(path_str).resolve().relative_to(APP.resolve())
    except ValueError, OSError:
        return None
    parts = rel.parts
    if len(parts) < 2:
        return None
    layer = parts[0]
    if layer not in LAYERS:
        return None
    mod_name = parts[1]
    if not (APP / layer / mod_name / "__init__.py").exists():
        return None
    return (layer, mod_name)


# Frame filenames that signal "internal" — importlib machinery, this audit
# module itself, frozen bootstrap. Skipped when walking the stack to find
# the importing user-code frame.
_INTERNAL_FRAME_MARKERS: tuple[str, ...] = (
    "<frozen importlib._bootstrap",
    "<frozen importlib._bootstrap_external",
    "importlib/_bootstrap.py",
    "importlib/_bootstrap_external.py",
    "importlib/util.py",
    "importlib/__init__.py",
    str(Path(__file__).resolve()),
)


def _is_internal_frame(filename: str) -> bool:
    return any(marker in filename for marker in _INTERNAL_FRAME_MARKERS)


def _find_importer_frame() -> tuple[str | None, str]:
    """Walk the stack upward; return (importer_filename, traceback_text).

    Skips frames internal to importlib or this audit module. The first
    non-internal frame is the importer; the traceback text lists up to 8
    user frames for the violation report.
    """
    frame: Any = sys._getframe(1)
    user_frames: list[tuple[str, int, str]] = []
    importer: str | None = None
    while frame is not None:
        filename = frame.f_code.co_filename
        if not _is_internal_frame(filename):
            user_frames.append((filename, frame.f_lineno, frame.f_code.co_name))
            if importer is None:
                importer = filename
        frame = frame.f_back

    if user_frames:
        tb = "\n".join(f"    {fn}:{ln} in {func}" for fn, ln, func in user_frames[:8])
    else:
        tb = "    <no user frames>"
    return importer, tb


class _ImportAuditFinder(MetaPathFinder):
    """Observe-only meta-path finder. ALWAYS returns None from find_spec.

    Records ``(importer, target)`` pairs that violate Rule-6 — cross-module
    submodule reaches — into the module-level accumulator. Never intercepts
    a real import; Python's normal finders resolve every module after this
    one returns None.
    """

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        # Fast path: anything outside app.* is invisible to the audit.
        if not fullname.startswith("app."):
            return None
        resolved = _resolve_module_target(fullname)
        if resolved is None:
            return None
        layer, mod_name, depth = resolved
        # depth == 0 = module-root import, always fine.
        if depth == 0:
            return None
        # depth > 0 = reaching into a submodule. Find who's importing.
        importer, tb = _find_importer_frame()
        if importer is None:
            importer = "<unknown>"
        owner = _owning_module_for_path(importer)
        # Within-module submodule import — fine.
        if owner == (layer, mod_name):
            return None
        # Cross-module submodule reach OR loose-file-to-submodule reach.
        # Both surface — no exemptions.
        try:
            importer_rel = str(Path(importer).resolve().relative_to(BACKEND.resolve()))
        except ValueError, OSError:
            importer_rel = importer
        key = (importer_rel, fullname)
        if key in _seen_keys:
            return None
        _seen_keys.add(key)
        _violations.add((importer_rel, fullname, tb))
        return None


def install() -> None:
    """Insert the audit finder at the front of ``sys.meta_path``.

    Writes ``tmp/import_audit_ran`` as a sentinel so ``bin/ci`` can prove
    the guard installed successfully. Idempotent: calling twice is a no-op.
    """
    for finder in sys.meta_path:
        if isinstance(finder, _ImportAuditFinder):
            return
    sys.meta_path.insert(0, _ImportAuditFinder())
    TMP.mkdir(parents=True, exist_ok=True)
    SENTINEL.write_text("1")


def flush_and_report() -> int:
    """Emit the violation report; return the violation count.

    On zero violations: removes any stale dump file, returns 0.
    On non-zero: writes a formatted report to stderr, writes JSON dump to
    ``tmp/import_audit_violations.json``, returns the count. Caller is
    responsible for overriding pytest's exit status when this is non-zero.
    """
    TMP.mkdir(parents=True, exist_ok=True)
    if not _violations:
        if DUMP.exists():
            DUMP.unlink()
        return 0
    items = sorted(_violations)
    payload = [{"importer": imp, "target": tgt, "stack": tb.splitlines()} for imp, tgt, tb in items]
    DUMP.write_text(json.dumps(payload, indent=2) + "\n")
    plural = "es" if len(items) != 1 else ""
    print(
        f"\nruntime import audit: {len(items)} cross-module submodule reach{plural} detected\n",
        file=sys.stderr,
    )
    for imp, tgt, tb in items:
        print(f"  {imp} imported {tgt}", file=sys.stderr)
        print(tb, file=sys.stderr)
        print("", file=sys.stderr)
    try:
        dump_rel = DUMP.relative_to(BACKEND)
    except ValueError:
        dump_rel = DUMP
    print(f"see {dump_rel} for machine-readable output", file=sys.stderr)
    return len(items)


def _reset_for_tests() -> None:
    """Clear the accumulator. Used only by this module's own self-tests."""
    _violations.clear()
    _seen_keys.clear()
