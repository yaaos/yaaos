"""Canary tests for check_table_access raw-SQL and suppression guards.

Each test injects a violation into a temp directory or a real module file,
runs the real enforcer, and asserts non-zero exit. try/finally restores any
modified files.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Load check_table_access (no .py extension).
# ---------------------------------------------------------------------------

_CTA_PATH = Path(__file__).parent / "check_table_access"
_spec = importlib.util.spec_from_loader(
    "check_table_access",
    importlib.machinery.SourceFileLoader("check_table_access", str(_CTA_PATH)),
)
assert _spec is not None and _spec.loader is not None
_cta: Any = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_table_access", _cta)
_spec.loader.exec_module(_cta)  # type: ignore[union-attr]

build_table_ownership_map = _cta.build_table_ownership_map
scan_raw_sql_violations = _cta.scan_raw_sql_violations
scan_tach_ignore = _cta.scan_tach_ignore
APP = Path(_cta.APP)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_temp_module(tmp_path: Path, rel_path: str, content: str) -> Path:
    """Write a temp .py file under a fake 'app/' structure mirroring the real one."""
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return full


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_raw_sql_foreign_table_rejected(tmp_path: Path) -> None:
    """text("SELECT * FROM tickets") in a non-tickets file → fail."""
    # Build a minimal ownership map from the real codebase.
    ownership = build_table_ownership_map()
    assert ownership, "ownership map must not be empty"
    assert "tickets" in ownership, "tickets table must be in ownership map"

    # Fake a non-tickets file that references the tickets table via raw SQL.
    fake_app = tmp_path / "app"
    fake_file = fake_app / "core" / "agent_gateway" / "bad.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text(
        textwrap.dedent("""\
            from sqlalchemy import text
            async def bad(session):
                await session.execute(text("SELECT * FROM tickets WHERE id = :id"), {"id": 1})
        """)
    )

    # Patch APP to the fake structure so scan_raw_sql_violations sees only our file.
    original_app = _cta.APP
    original_allowlist = _cta.ALLOWLIST_PREFIX
    try:
        _cta.APP = fake_app
        _cta.ALLOWLIST_PREFIX = fake_app / "core" / "database"
        violations = scan_raw_sql_violations(ownership)
    finally:
        _cta.APP = original_app
        _cta.ALLOWLIST_PREFIX = original_allowlist

    assert violations, "expected violation for cross-module raw SQL but got none"
    assert any("tickets" in v for v in violations), (
        f"expected 'tickets' in violation message but got: {violations}"
    )


def test_non_literal_text_arg_rejected(tmp_path: Path) -> None:
    """text(f"… {t}") → fail (non-literal args are never allowed)."""
    ownership = build_table_ownership_map()
    assert ownership

    fake_app = tmp_path / "app"
    fake_file = fake_app / "domain" / "reviewer" / "bad.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text(
        textwrap.dedent("""\
            from sqlalchemy import text
            async def bad(session, t):
                await session.execute(text(f"SELECT * FROM orgs WHERE id = {t}"))
        """)
    )

    original_app = _cta.APP
    original_allowlist = _cta.ALLOWLIST_PREFIX
    try:
        _cta.APP = fake_app
        _cta.ALLOWLIST_PREFIX = fake_app / "core" / "database"
        violations = scan_raw_sql_violations(ownership)
    finally:
        _cta.APP = original_app
        _cta.ALLOWLIST_PREFIX = original_allowlist

    assert violations, "expected violation for non-literal text() arg but got none"
    assert any("non-literal" in v for v in violations), (
        f"expected 'non-literal' in violation message but got: {violations}"
    )


def test_unimportable_models_fails_closed(tmp_path: Path) -> None:
    """An AST-unparseable models.py → empty ownership map → fail closed."""
    # Use a real models.py as the injection target.
    target = APP / "domain" / "tickets" / "models.py"
    original = target.read_text(encoding="utf-8")
    try:
        # Corrupt the file so AST parsing returns an empty map.
        target.write_text("this is not valid python :::SYNTAX ERROR:::")
        result = build_table_ownership_map()
        # Empty map → main() returns 1.
        assert result == {}, f"expected empty map from unparseable models.py, got {result}"
    finally:
        target.write_text(original)


def test_tach_ignore_directive_rejected(tmp_path: Path) -> None:
    """A # tach-ignore anywhere under app/ → fail."""
    fake_app = tmp_path / "app"
    fake_file = fake_app / "domain" / "orgs" / "bad.py"
    fake_file.parent.mkdir(parents=True, exist_ok=True)
    fake_file.write_text(
        textwrap.dedent("""\
            from app.domain.tickets import Ticket  # tach-ignore
        """)
    )

    hits = scan_tach_ignore(fake_app)
    assert hits, "expected tach-ignore hit but got none"
    assert any("tach-ignore" in h for h in hits), f"expected 'tach-ignore' in hit message but got: {hits}"
