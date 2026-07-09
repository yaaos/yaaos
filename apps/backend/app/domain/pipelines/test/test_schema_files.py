"""Drift guard for the committed skill-contract JSON Schema files.

`.claude/skills/pipeline-schemas/*.schema.json` are hand-maintained copies
of `SkillReturn` / `SkillReviewReturn` for local skill runs and human
wrappers — the engine-injected schema stays authoritative at runtime. This
test enforces the maintenance promise: change the contract, change the file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.pipelines.contracts import SkillReturn, SkillReviewReturn

_SCHEMAS_DIR = Path(__file__).parents[6] / ".claude" / "skills" / "pipeline-schemas"


@pytest.mark.parametrize(
    ("filename", "model"),
    [
        ("skill-return.schema.json", SkillReturn),
        ("skill-review-return.schema.json", SkillReviewReturn),
    ],
)
def test_committed_schema_matches_model(filename: str, model: type) -> None:
    path = _SCHEMAS_DIR / filename
    assert path.is_file(), f"missing committed schema file: {path}"
    committed = json.loads(path.read_text())
    assert committed == model.model_json_schema(), (
        f"{filename} has drifted from {model.__name__}.model_json_schema() — "
        "update the committed file to match the contract"
    )
