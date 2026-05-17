"""Re-review parser + skip-path heuristics — pure logic."""

import pytest

from app.domain.intake import is_skippable_path, parse_rereview


@pytest.mark.parametrize(
    "body",
    [
        "hey @yaaos rereview please",
        # Legacy `@yaaos-<specialty>` form still matches; specialty is ignored.
        "hello @yaaos-architecture rereview",
        "@yaaos-security rereview thanks",
        "@YAAOS-style ReReview",
    ],
)
def test_rereview_parses(body: str) -> None:
    matched, agent = parse_rereview(body)
    assert matched
    assert agent is None


def test_no_match() -> None:
    matched, _ = parse_rereview("nothing here")
    assert not matched


def test_no_match_for_wrong_command() -> None:
    matched, _ = parse_rereview("@yaaos review")
    assert not matched


@pytest.mark.parametrize(
    "path",
    [
        "package-lock.json",
        "yarn.lock",
        "node_modules/foo/bar.js",
        "vendor/somelib.go",
        "image.png",
        "build/output.js",
        "src/types_generated.ts",
        "proto/foo.pb.go",
    ],
)
def test_skippable_paths(path: str) -> None:
    assert is_skippable_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/user.ts",
        "app/main.py",
        "README.md",
    ],
)
def test_nonskippable_paths(path: str) -> None:
    assert not is_skippable_path(path)
