"""Shared logic for building and serialising the committed web-api OpenAPI artifact.

Used by both `apps/backend/bin/dump_web_openapi` (writes the file) and the
drift-gate pytest (`test/test_web_openapi_drift.py`, which compares the
in-memory result to the committed file).

The caller is responsible for importing `app.web` before calling these
functions — that side-effect registers every RouteSpec so that
`build_stripped_spec()` sees the full route set.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from app.core.webserver.app_factory import mount_specs

_TESTING_PATH_RE = re.compile(r"^/api/testing/")

ARTIFACT_PATH = Path(__file__).resolve().parents[3] / "openapi" / "web-api.json"


def build_stripped_spec() -> dict[str, Any]:
    """Build the full OpenAPI spec from the currently-registered routes and
    strip every path whose URL starts with ``/api/testing/``.

    Caller must have imported ``app.web`` first (side-effect: registers all
    RouteSpecs). This function does NOT import ``app.web`` itself so it stays
    importable from tests that manage that import themselves.
    """
    app = FastAPI(title="yaaos", version="0.0.1")
    mount_specs(app)

    spec: dict[str, Any] = app.openapi()  # type: ignore[assignment]

    # Strip test-only backdoor paths before serialising.
    paths: dict[str, Any] = spec.get("paths", {})
    stripped = {path: item for path, item in paths.items() if not _TESTING_PATH_RE.match(path)}
    spec["paths"] = stripped
    return spec


def serialise_spec(spec: dict[str, Any]) -> str:
    """Serialise *spec* to a stable, deterministic JSON string.

    Keys are sorted recursively; output ends with a single trailing newline.
    """
    return json.dumps(spec, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
