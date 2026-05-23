"""M06 status vocabulary + projection from the legacy `tickets.status` enum.

The legacy ticket lifecycle has 4 states (open / in_review / complete /
abandoned); M06 surfaces a 5-state display vocabulary
(running / hitl / done / failed / cancelled) that's friendlier in the UI.

The accurate projection wants a join through `workflow_executions` to
catch `hitl` and `failed` precisely — that lives in
`apps/backend/app/domain/reviewer/workflow_review_view.py` once Phase 5 of
M06 wires it. The POC mapping here is the conservative fallback the
Tickets list / Dashboard projections use until then: terminal statuses
map cleanly, in-flight maps to `running`.
"""

from __future__ import annotations

from typing import Literal

LegacyStatus = Literal["open", "in_review", "complete", "abandoned"]
M06Status = Literal["running", "hitl", "done", "failed", "cancelled"]


def project_status(legacy: str) -> M06Status:
    """Map a legacy `tickets.status` value to the M06 display vocabulary.

    The mapping is loss-free for terminal states; in-flight states collapse
    into `running` until the workflow-state join lands.
    """
    if legacy == "complete":
        return "done"
    if legacy == "abandoned":
        return "cancelled"
    # open / in_review — both surface as "running" in the SPA until the
    # workflow-state join distinguishes hitl / failed.
    return "running"
