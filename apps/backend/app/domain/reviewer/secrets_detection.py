"""Secret-pattern detection for PR diffs.

Used by the reviewer pipeline to short-circuit a review when a known
secret pattern lands in `+`-prefixed (added) lines. Returns the rule id
of the first match so the caller can surface a "secrets-detected" warning
comment instead of running the agent over leaked credentials.

Patterns are deliberately conservative — match obvious shapes (`AKIA…`,
`ghp_…`, `-----BEGIN … PRIVATE KEY-----`) and stop. No regex-tuning
beyond what catches the canonical formats; false negatives on disguised
secrets are tolerable.
"""

from __future__ import annotations

import re

from app.core.vcs import Diff

# Order matters only insofar as we return the first match — pick more
# specific patterns first if you add overlapping rules.
_SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("private_key_pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

# AWS-published example access-key IDs. Documented placeholders that appear in
# AWS's own IAM docs and in countless tutorials/fixtures; matching the AKIA
# regex but never real credentials. Skip them so PRs that ship AWS doc snippets
# or secrets-scan test fixtures aren't refused.
_KNOWN_FAKE_SECRETS: frozenset[str] = frozenset(
    {
        "AKIAIOSFODNN7EXAMPLE",
        "AKIAI44QH8DHBEXAMPLE",
    }
)


def detect_secrets(diff: Diff) -> str | None:
    """Return the first secret-rule id matched by an added line in `diff`,
    or None when no secret pattern is observed.

    Only `+`-prefixed lines (excluding `+++` filename headers) are scanned
    — a removed secret isn't a leak going forward. Matches whose value is in
    `_KNOWN_FAKE_SECRETS` are skipped and scanning continues.
    """
    for raw_line in (diff.raw or "").splitlines():
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        for rule_id, pat in _SECRET_RULES:
            for match in pat.finditer(raw_line):
                if match.group(0) not in _KNOWN_FAKE_SECRETS:
                    return rule_id
    return None


def secrets_warning_body(rule_id: str) -> str:
    """Return the warning comment body for a secrets-detected PR.

    Posted via `vcs.post_comment` as a top-level PR comment; yaaos refuses
    to review the diff and instructs the author to remediate.
    """
    return (
        "yaaos refused to review this PR — the diff contains content that "
        f"looks like a leaked secret (rule: `{rule_id}`). Remove the secret, "
        "rotate it on the upstream provider, then push a fresh commit and the "
        "review will run automatically."
    )


__all__ = ["detect_secrets", "secrets_warning_body"]
