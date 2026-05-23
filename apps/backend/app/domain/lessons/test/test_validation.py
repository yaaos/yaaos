"""Pure-validation tests (no DB)."""

import pytest

from app.domain.lessons.service import LessonValidationError, _validate


def test_empty_title_rejected() -> None:
    with pytest.raises(LessonValidationError):
        _validate("", "body")


def test_empty_body_rejected() -> None:
    with pytest.raises(LessonValidationError):
        _validate("title", "")


def test_long_body_rejected() -> None:
    with pytest.raises(LessonValidationError):
        _validate("ok", "x" * 1001)


def test_long_title_rejected() -> None:
    with pytest.raises(LessonValidationError):
        _validate("x" * 201, "body")


def test_valid_passes() -> None:
    _validate("Use the helper", "Apply consistently.")
