"""Trigger policy for auto-incremental reviews on PR push (plan §7).

Pure function: given the PR state, in-flight reviews, last reviewed commit,
new head, recent push timestamps, and the new commits in the diff, decide
whether to run an incremental review now, debounce, or skip.

Manual full review bypasses this entirely (callers go straight to
`schedule_full_review`); this module decides what auto-incremental does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.domain.reviewer.types import ReviewScope

# Skip reasons (POC set, plan §7). Strings, not an enum, so audit payloads and
# log lines stay grep-friendly.
SkipReason = Literal[
    "draft",
    "history_changed",
    "base_merged",
    "in_flight",
]


@dataclass(frozen=True)
class TriggerInputs:
    """Everything `decide_trigger` needs to make a call. Owned by `service.py`."""

    pr_is_draft: bool
    last_reviewed_sha: str | None
    head_sha: str
    in_flight_review_id: str | None
    new_commit_messages: list[str]
    last_reviewed_sha_is_ancestor: bool
    last_push_at: datetime | None
    now: datetime
    debounce_window_seconds: int = 30


@dataclass(frozen=True)
class Skip:
    reason: SkipReason


@dataclass(frozen=True)
class Debounce:
    seconds_remaining: float


@dataclass(frozen=True)
class Run:
    scope: ReviewScope


TriggerDecision = Skip | Debounce | Run


def decide_trigger(inputs: TriggerInputs) -> TriggerDecision:
    """First-match-wins evaluation per plan §7.

    Rules:
    1. PR is draft → Skip("draft").
    2. Last-reviewed SHA isn't an ancestor of head → Skip("history_changed").
    3. Any new commit looks like a merge from base → Skip("base_merged").
    4. A review is already in flight → Skip("in_flight").
       (Caller separately flips `pending_replay = true` on the in-flight row;
       this module is decision-only.)
    5. Push within the debounce window → Debounce(remaining).
    6. Otherwise → Run(Incremental(last_reviewed_sha..head)).

    First push (no `last_reviewed_sha`) returns `Skip("history_changed")` so
    callers route through the manual full review path; auto-incremental only
    handles subsequent pushes.
    """
    if inputs.pr_is_draft:
        return Skip(reason="draft")

    if inputs.last_reviewed_sha is None or not inputs.last_reviewed_sha_is_ancestor:
        return Skip(reason="history_changed")

    if _includes_base_merge(inputs.new_commit_messages):
        return Skip(reason="base_merged")

    if inputs.in_flight_review_id is not None:
        return Skip(reason="in_flight")

    if inputs.last_push_at is not None:
        elapsed = (inputs.now - inputs.last_push_at).total_seconds()
        remaining = inputs.debounce_window_seconds - elapsed
        if remaining > 0:
            return Debounce(seconds_remaining=remaining)

    return Run(scope=ReviewScope.incremental(prev_sha=inputs.last_reviewed_sha, head_sha=inputs.head_sha))


def _includes_base_merge(new_commit_messages: list[str]) -> bool:
    """Heuristic: any commit message starting with "Merge branch '" or "Merge remote-tracking branch '"."""
    return any(
        m.startswith("Merge branch '")
        or m.startswith("Merge remote-tracking branch '")
        or m.startswith("Merge pull request ")
        for m in new_commit_messages
    )


def humanize_skip(reason: SkipReason) -> str:
    return {
        "draft": "PR is a draft",
        "history_changed": "history changed since last review (force-push or rebase); run a manual full review",
        "base_merged": "new commits include a base-branch merge; run a manual full review",
        "in_flight": "a review is already in flight for this PR",
    }[reason]


__all__ = [
    "Debounce",
    "Run",
    "Skip",
    "SkipReason",
    "TriggerDecision",
    "TriggerInputs",
    "decide_trigger",
    "humanize_skip",
]
