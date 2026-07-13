"""Drift guard for the committed ArtifactFrontmatter JSON Schema file.

`.claude/skills/pipeline-schemas/artifact-frontmatter.schema.json` is a
hand-maintained copy of `ArtifactFrontmatter` for local skill runs and human
wrappers — the model itself is authoritative at runtime. This test enforces the
maintenance promise: change the contract, change the file.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.domain.attachments.contracts import ArtifactFrontmatter

_SCHEMAS_DIR = Path(__file__).parents[6] / ".claude" / "skills" / "pipeline-schemas"


def test_committed_schema_matches_model() -> None:
    path = _SCHEMAS_DIR / "artifact-frontmatter.schema.json"
    assert path.is_file(), f"missing committed schema file: {path}"
    committed = json.loads(path.read_text())
    assert committed == ArtifactFrontmatter.model_json_schema(), (
        "artifact-frontmatter.schema.json has drifted from "
        "ArtifactFrontmatter.model_json_schema() — "
        "update the committed file to match the contract"
    )
