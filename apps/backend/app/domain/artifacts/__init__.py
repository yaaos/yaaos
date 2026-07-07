"""domain/artifacts — produced-document storage; one row per artifact version.

No descriptor/lineage entity — the lineage ("the ticket's requirements
document") is the `(ticket_id, stage_name)` group. Read-only for humans;
revisions arrive only via instruct/re-run on a later engine phase.
"""

from app.domain.artifacts.service import (
    ArtifactNotFoundError,
    get,
    latest_final,
    list_for_ticket,
    mark_final,
    store,
)
from app.domain.artifacts.types import Artifact, ArtifactGroup, ArtifactMeta

__all__ = [
    "Artifact",
    "ArtifactGroup",
    "ArtifactMeta",
    "ArtifactNotFoundError",
    "get",
    "latest_final",
    "list_for_ticket",
    "mark_final",
    "store",
]
