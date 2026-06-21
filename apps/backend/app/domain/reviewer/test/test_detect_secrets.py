"""Unit tests for the pre-flight secrets detector.

Pure tests — no DB, no HTTP. The detector only inspects `Diff.raw` and only
scans added lines (so a removed secret doesn't trigger).
"""

from __future__ import annotations

from app.core.vcs import Diff
from app.domain.reviewer.secrets_detection import detect_secrets as _detect_secrets


def _diff(raw: str) -> Diff:
    return Diff(raw=raw, files=[])


def test_detects_aws_access_key_in_added_line() -> None:
    raw = "diff --git a/.env b/.env\n+++ b/.env\n+AWS_KEY=AKIAQWERTYUIOPASDFGH\n"
    assert _detect_secrets(_diff(raw)) == "aws_access_key"


def test_detects_github_token() -> None:
    raw = "+token: ghp_" + "a" * 36 + "\n"
    assert _detect_secrets(_diff(raw)) == "github_token"


def test_detects_anthropic_key() -> None:
    raw = "+ANTHROPIC=sk-ant-" + "a" * 50 + "\n"
    assert _detect_secrets(_diff(raw)) == "anthropic_key"


def test_detects_private_key_block() -> None:
    raw = "+-----BEGIN RSA PRIVATE KEY-----\n"
    assert _detect_secrets(_diff(raw)) == "private_key_pem"


def test_ignores_secret_on_removed_line() -> None:
    raw = "-AWS_KEY=AKIAQWERTYUIOPASDFGH\n"
    assert _detect_secrets(_diff(raw)) is None


def test_ignores_secret_in_context_line() -> None:
    raw = " AWS_KEY=AKIAQWERTYUIOPASDFGH\n"
    assert _detect_secrets(_diff(raw)) is None


def test_ignores_diff_header_marker() -> None:
    raw = "+++ b/secrets.txt with AKIAQWERTYUIOPASDFGH inline\n"
    # `+++` lines are file headers, not added content.
    assert _detect_secrets(_diff(raw)) is None


def test_returns_none_when_no_match() -> None:
    raw = "+just a normal added line\n-removed\n context\n"
    assert _detect_secrets(_diff(raw)) is None


def test_allowlists_aws_published_example_keys() -> None:
    # AWS's own IAM docs use these placeholders; they match the AKIA regex
    # but are not real credentials. Scanning continues past them.
    raw = "+AWS_KEY=AKIAIOSFODNN7EXAMPLE\n+OTHER_KEY=AKIAI44QH8DHBEXAMPLE\n"
    assert _detect_secrets(_diff(raw)) is None


def test_allowlist_does_not_shield_a_real_match_on_the_same_line() -> None:
    # An allowlisted value next to a non-allowlisted one must still fire.
    raw = "+pair=AKIAIOSFODNN7EXAMPLE,AKIAQWERTYUIOPASDFGH\n"
    assert _detect_secrets(_diff(raw)) == "aws_access_key"
