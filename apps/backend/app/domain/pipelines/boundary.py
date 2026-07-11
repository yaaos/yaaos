"""Boundary evaluator for `domain/pipelines` — decides pause vs proceed once
a stage's own main/review work has settled.

`cannot_complete`/`send_back` main-skill outcomes never reach `evaluate_boundary`
— they fail the stage earlier, before any artifact or residual concept
exists (see `_validate_skill_return_and_artifact` in `engine.py`), so by the
time this runs the stage's own work always "completed" and only
mode/conditions remain. `always_hitl`/`always_proceed` short-circuit before
any condition is read. `conditional` accumulates every tripped condition
(rather than stopping at the first hit) so a pause record shows every
reason a human needs to look, not just one:

- `on_blocker_residuals` / `on_should_fix_residuals` / `on_nit_residuals`
  against `residuals`' severities. A review-off stage always passes an empty
  `residuals` — the checkboxes structurally can't fire (no review ran, so no
  findings exist).
- `on_protected_code` against `repos.evaluate_protected(paths_affected)`.
  Skipped when `paths_affected` is empty — a standalone `ReviewSkillStage`'s
  `SkillReviewReturn` carries no such field, so the condition can never
  trip for it.
- `on_confidence_below` against the stage's bucketed confidence (the last
  main-skill `SkillReturn.confidence`, or the review confidence for a
  standalone review stage).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.findings import Finding
from app.domain.pipelines.contracts import Confidence
from app.domain.pipelines.definition import BoundaryControl
from app.domain.repos import evaluate_protected

_CONFIDENCE_RANK: dict[Confidence, int] = {"low": 0, "medium": 1, "high": 2}


class BoundaryDecision(BaseModel, frozen=True):
    """The evaluator's answer. `tripped` is the full set of conditions that
    fired (empty on `proceed`) — persisted verbatim on both
    `run_pauses.tripped` and `stage_executions.boundary_detail`.
    `protected_owner_user_ids` folds into the pause's escalation set only
    when `on_protected_code` fired."""

    outcome: Literal["proceed", "pause"]
    tripped: dict[str, Any]
    protected_owner_user_ids: tuple[UUID, ...] = ()


async def evaluate_boundary(
    control: BoundaryControl,
    *,
    org_id: UUID,
    repo_external_id: str,
    residuals: Sequence[Finding],
    paths_affected: Sequence[str],
    confidence: Confidence,
    session: AsyncSession,
) -> BoundaryDecision:
    if control.mode == "always_proceed":
        return BoundaryDecision(outcome="proceed", tripped={})
    if control.mode == "always_hitl":
        return BoundaryDecision(outcome="pause", tripped={"always_hitl": True})

    tripped: dict[str, Any] = {}
    if control.on_blocker_residuals and any(f.severity == "blocker" for f in residuals):
        tripped["blocker_residuals"] = True
    if control.on_should_fix_residuals and any(f.severity == "should_fix" for f in residuals):
        tripped["should_fix_residuals"] = True
    if control.on_nit_residuals and any(f.severity == "nit" for f in residuals):
        tripped["nit_residuals"] = True

    protected_owner_user_ids: tuple[UUID, ...] = ()
    if control.on_protected_code and paths_affected:
        match = await evaluate_protected(org_id, repo_external_id, paths_affected, session=session)
        if match.matched:
            tripped["protected_code"] = True
            protected_owner_user_ids = match.owner_user_ids

    if control.on_confidence_below is not None:
        if _CONFIDENCE_RANK[confidence] < _CONFIDENCE_RANK[control.on_confidence_below]:
            tripped["confidence_below"] = control.on_confidence_below

    if tripped:
        return BoundaryDecision(
            outcome="pause", tripped=tripped, protected_owner_user_ids=protected_owner_user_ids
        )
    return BoundaryDecision(outcome="proceed", tripped={})
