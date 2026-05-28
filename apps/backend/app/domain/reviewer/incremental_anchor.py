"""Anchor resolution + stale-check context for incremental review.

Pure helpers for incremental review. The
`IncrementalReview` engine command imports these to run the
deterministic anchor pass and to build the per-finding `StaleCheckContext`
that drives the LLM `stale_check` agent call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.domain.coding_agent import StaleCheckContext
from app.domain.reviewer.aggregate import PRReviewAggregate
from app.domain.reviewer.anchor import resolve_anchor


@dataclass
class ResolveAnchorsResult:
    """What `resolve_open_anchors` did, partitioned by outcome.

    - `moved`: anchor block now lives at a new line range. Caller should
      run `verify_fix` on these.
    - `gone`: surrounding hash isn't in the new content (file deleted, or
      block removed/heavily edited). Aggregate state is set to
      `resolved_unverified`; LLM `stale_check` can still transition further.
    - `unchanged`: anchor still sits at the same line range; commit_sha is
      refreshed for bookkeeping.
    """

    moved: list[UUID] = field(default_factory=list)
    gone: list[UUID] = field(default_factory=list)
    unchanged: list[UUID] = field(default_factory=list)


def resolve_open_anchors(
    aggregate: PRReviewAggregate,
    *,
    touched_files: set[str],
    read_file: Callable[[str], list[str] | None],
    new_commit_sha: str,
) -> ResolveAnchorsResult:
    """Re-resolve anchors for every open finding in `touched_files`.

    Pure helper — no I/O of its own. Caller supplies `read_file` (which
    inside the engine command reads from the workspace; in tests is a
    pure dict lookup). Deterministic anchor lookup before the LLM
    stale-check fires.
    """
    out = ResolveAnchorsResult()
    for finding in aggregate.open_findings_in_files(touched_files):
        new_lines = read_file(finding.current_anchor.file_path)
        if new_lines is None:
            aggregate.mark_unverified_resolution(finding.id)
            out.gone.append(finding.id)
            continue
        new_anchor = resolve_anchor(finding.current_anchor, new_lines, new_commit_sha)
        if new_anchor is None:
            aggregate.mark_unverified_resolution(finding.id)
            out.gone.append(finding.id)
            continue
        if (
            new_anchor.line_start == finding.current_anchor.line_start
            and new_anchor.line_end == finding.current_anchor.line_end
        ):
            out.unchanged.append(finding.id)
            continue
        aggregate.update_anchor(finding.id, new_anchor)
        out.moved.append(finding.id)
    return out


def stale_check_context_for(finding: Any, diff: Any) -> StaleCheckContext:
    """Build a `StaleCheckContext` from a Finding + the incremental diff.

    The agent has the workspace and reads the file itself, so we hand it
    finding metadata and a brief diff summary. No file content here.
    """
    file_path = finding.current_anchor.file_path
    matched = next((f for f in (diff.files or []) if f.path == file_path), None)
    summary = (
        f"{file_path}: +{matched.additions}/-{matched.deletions} lines"
        if matched is not None
        else f"{file_path}: not in this diff"
    )
    return StaleCheckContext(
        original_finding_title=finding.title,
        original_finding_body=finding.body,
        original_rule_id=finding.rule_id,
        current_code_snippet=(
            f"see {file_path} at lines {finding.current_anchor.line_start}-{finding.current_anchor.line_end}"
        ),
        diff_summary=summary,
        agent_config={},
    )


__all__ = [
    "ResolveAnchorsResult",
    "resolve_open_anchors",
    "stale_check_context_for",
]
