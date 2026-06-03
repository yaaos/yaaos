"""Drift gate: committed web-api.json must match what the current code produces.

If this test fails, re-run `apps/backend/bin/dump_web_openapi` and commit the
updated artifact:

    uv run python apps/backend/bin/dump_web_openapi
    git add apps/backend/openapi/web-api.json
    git commit
"""

from __future__ import annotations

import json

import app.web  # noqa: F401 — side-effect: registers every RouteSpec
from app.core.webserver.openapi_artifact import (
    ARTIFACT_PATH,
    build_stripped_spec,
)


def test_committed_web_openapi_matches_current_code() -> None:
    """Re-generate the stripped spec in memory and assert it equals the
    committed ``openapi/web-api.json``.

    Fails with a clear message telling the developer which script to run.
    """
    assert ARTIFACT_PATH.exists(), (
        f"Committed artifact {ARTIFACT_PATH} is missing. "
        f"Run `uv run python apps/backend/bin/dump_web_openapi` from the repo root "
        f"and commit the result."
    )

    expected_raw = ARTIFACT_PATH.read_text(encoding="utf-8")
    expected = json.loads(expected_raw)

    live = build_stripped_spec()

    if live != expected:
        # Build a human-readable diff summary: which paths differ.
        live_paths = set(live.get("paths", {}).keys())
        committed_paths = set(expected.get("paths", {}).keys())
        added = sorted(live_paths - committed_paths)
        removed = sorted(committed_paths - live_paths)
        schema_changed = live != expected and not added and not removed

        detail_parts: list[str] = []
        if added:
            detail_parts.append(f"paths added in code but not in artifact: {added}")
        if removed:
            detail_parts.append(f"paths in artifact but removed from code: {removed}")
        if schema_changed:
            detail_parts.append("schema/response-model change (no path-level diff)")

        detail = "; ".join(detail_parts) if detail_parts else "spec differs"

        raise AssertionError(
            f"openapi/web-api.json is stale ({detail}). "
            f"Run `uv run python apps/backend/bin/dump_web_openapi` from the repo "
            f"root and commit the updated artifact."
        )


def test_no_testing_paths_in_artifact() -> None:
    """Belt-and-suspenders: confirm the committed artifact has no /api/testing/* paths."""
    spec = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    testing_paths = [p for p in spec.get("paths", {}) if p.startswith("/api/testing/")]
    assert not testing_paths, (
        f"Committed web-api.json contains test-only paths that must be stripped: "
        f"{testing_paths}. Re-run `bin/dump_web_openapi` and commit."
    )
