"""Drift guard: every skill name referenced by the shipped pipeline
templates (`domain/pipelines/defaults.py`) has a corresponding
`.claude/skills/<name>/SKILL.md` file in the repo. Skill names are derived
by walking the template stage objects, not from a hardcoded list — a
reference added to a template without shipping the skill file fails this
test instead of failing silently at run time.
"""

from __future__ import annotations

from pathlib import Path

from app.domain.pipelines.defaults import ALL_DEFAULTS
from app.domain.pipelines.definition import ReviewSkillStage, SkillStage

_REPO_ROOT = Path(__file__).parents[6]


def _referenced_skill_names() -> set[str]:
    names: set[str] = set()
    for definition in ALL_DEFAULTS:
        for stage in definition.stages:
            if isinstance(stage, SkillStage):
                names.add(stage.skill_name)
                if stage.review is not None:
                    names.add(stage.review.skill_name)
            elif isinstance(stage, ReviewSkillStage):
                names.add(stage.skill_name)
    return names


def test_every_template_skill_reference_has_a_shipped_skill_file() -> None:
    names = _referenced_skill_names()
    assert names, "expected at least one skill_name across the shipped templates"
    missing = [
        name
        for name in sorted(names)
        if not (_REPO_ROOT / ".claude" / "skills" / name / "SKILL.md").is_file()
    ]
    assert not missing, f"templates reference skills with no shipped SKILL.md: {missing}"
