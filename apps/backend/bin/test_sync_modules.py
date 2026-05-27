"""Unit tests for sync_modules.parse_module_interface.

Tests are co-located with the script they cover. No DB, no network — pure AST
parsing logic.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any

# sync_modules has no .py extension; load it via source loader explicitly.
_SYNC_MODULES_PATH = Path(__file__).parent / "sync_modules"
_spec = importlib.util.spec_from_loader(
    "sync_modules",
    importlib.machinery.SourceFileLoader("sync_modules", str(_SYNC_MODULES_PATH)),
)
assert _spec is not None and _spec.loader is not None
_sync_modules: Any = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("sync_modules", _sync_modules)
_spec.loader.exec_module(_sync_modules)  # type: ignore[union-attr]

parse_module_interface = _sync_modules.parse_module_interface


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_init(tmp_path: Path, source: str) -> tuple[str, str]:
    """Write source to a fake module __init__.py; return (layer, name)."""
    layer = "core"
    name = "fake_mod"
    mod_dir = tmp_path / "app" / layer / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "__init__.py").write_text(textwrap.dedent(source))
    return layer, name


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_multiline_all_returns_sorted(tmp_path: Path) -> None:
    """Multi-line __all__ is parsed and returned in sorted order."""
    original_app = _sync_modules.APP
    _sync_modules.APP = tmp_path / "app"
    try:
        layer, name = _write_init(
            tmp_path,
            """\
            __all__ = [
                "zebra",
                "apple",
                "mango",
            ]
            """,
        )
        result = parse_module_interface(layer, name)
        assert result == ["apple", "mango", "zebra"]
    finally:
        _sync_modules.APP = original_app


def test_inline_all_returns_sorted(tmp_path: Path) -> None:
    """Single-line __all__ is parsed and returned in sorted order."""
    original_app = _sync_modules.APP
    _sync_modules.APP = tmp_path / "app"
    try:
        layer, name = _write_init(
            tmp_path,
            '__all__ = ["beta", "alpha"]\n',
        )
        result = parse_module_interface(layer, name)
        assert result == ["alpha", "beta"]
    finally:
        _sync_modules.APP = original_app


def test_empty_all_returns_empty_list(tmp_path: Path) -> None:
    """Empty __all__ = [] returns []."""
    original_app = _sync_modules.APP
    _sync_modules.APP = tmp_path / "app"
    try:
        layer, name = _write_init(tmp_path, "__all__ = []\n")
        result = parse_module_interface(layer, name)
        assert result == []
    finally:
        _sync_modules.APP = original_app


def test_missing_all_returns_empty_list(tmp_path: Path) -> None:
    """No __all__ in the file returns []."""
    original_app = _sync_modules.APP
    _sync_modules.APP = tmp_path / "app"
    try:
        layer, name = _write_init(tmp_path, "x = 1\n")
        result = parse_module_interface(layer, name)
        assert result == []
    finally:
        _sync_modules.APP = original_app


def test_missing_init_returns_empty_list(tmp_path: Path) -> None:
    """Module with no __init__.py returns []."""
    original_app = _sync_modules.APP
    _sync_modules.APP = tmp_path / "app"
    try:
        result = parse_module_interface("core", "nonexistent")
        assert result == []
    finally:
        _sync_modules.APP = original_app
