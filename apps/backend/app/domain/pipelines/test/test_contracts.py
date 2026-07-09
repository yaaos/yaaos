"""Unit tests for `domain/pipelines.contracts` — the skill wire contracts.

`SkillReviewFinding.category` is the finding's display prefix: one lowercase
word, 2-12 letters, no separators. The pattern rides the injected JSON
Schema, so a violating value fails stage validation the same way any other
schema violation does.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.pipelines.contracts import SkillReviewFinding


def _finding(category: str) -> SkillReviewFinding:
    return SkillReviewFinding(category=category, severity="nit", body="a finding")


@pytest.mark.parametrize("category", ["sec", "arch", "code", "perf", "test", "completeness"])
def test_category_accepts_lowercase_words(category: str) -> None:
    assert _finding(category).category == category


@pytest.mark.parametrize(
    "category",
    ["", "a", "Sec", "sec-review", "sec 1", "sec1", "toolongtobeacategory"],
)
def test_category_rejects_non_lowercase_word_shapes(category: str) -> None:
    with pytest.raises(ValidationError):
        _finding(category)


def test_category_is_required() -> None:
    with pytest.raises(ValidationError):
        SkillReviewFinding(severity="nit", body="a finding")  # type: ignore[call-arg]
