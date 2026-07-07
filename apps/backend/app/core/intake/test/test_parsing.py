"""Re-review parser + skip-path heuristics — pure logic."""

import pytest

from app.core.intake import is_skippable_path, parse_rereview, parse_yaaos_command


@pytest.mark.parametrize(
    "body",
    [
        "hey @yaaos rereview please",
        # The `@yaaos-<specialty>` form still matches; specialty is ignored.
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
    "body",
    ["@yaaos re-review", "@yaaos RE-REVIEW please", "hey @yaaos re-review"],
)
def test_parse_yaaos_command_re_review(body: str) -> None:
    assert parse_yaaos_command(body) == "re-review"


@pytest.mark.parametrize(
    "body",
    ["hey @yaaos rereview please", "@yaaos-architecture rereview"],
)
def test_parse_yaaos_command_legacy_rereview_maps_to_re_review(body: str) -> None:
    assert parse_yaaos_command(body) == "re-review"


def test_parse_yaaos_command_cancel() -> None:
    assert parse_yaaos_command("@yaaos cancel") == "cancel"


def test_parse_yaaos_command_full_review_no_longer_recognized() -> None:
    """`full review` is retired — the token set is `re-review | cancel`."""
    assert parse_yaaos_command("@yaaos full review") is None


def test_parse_yaaos_command_plain_review_no_longer_recognized() -> None:
    assert parse_yaaos_command("@yaaos review") is None


def test_parse_yaaos_command_no_match() -> None:
    assert parse_yaaos_command("just a regular comment") is None


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
