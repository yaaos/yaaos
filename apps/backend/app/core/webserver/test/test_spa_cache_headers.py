"""SPA cache headers: /assets/* gets immutable, index.html gets short revalidate.

Boots a minimal FastAPI app with just `_install_spa_serving` mounted —
avoids the full lifespan (migrations etc.). Skips if `apps/web/dist`
isn't built (dev workflow without a recent `pnpm build`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.webserver.app_factory import _install_spa_serving

_DIST = Path(__file__).resolve().parents[5] / "web" / "dist"
_ASSETS = _DIST / "assets"


pytestmark = pytest.mark.skipif(
    not _ASSETS.exists(),
    reason="apps/web/dist/assets not built — run `pnpm build` to enable",
)


def _client() -> TestClient:
    app = FastAPI()
    _install_spa_serving(app)
    return TestClient(app)


def test_assets_get_immutable_year_long_cache() -> None:
    # Pick any real asset Vite emitted.
    sample = next(_ASSETS.iterdir())
    with _client() as client:
        r = client.get(f"/assets/{sample.name}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_index_html_gets_short_revalidate() -> None:
    with _client() as client:
        r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=60, must-revalidate"


def test_dist_root_real_file_also_revalidates() -> None:
    # favicon.svg is copied by Vite from apps/web/public/ to dist root.
    with _client() as client:
        r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=60, must-revalidate"
