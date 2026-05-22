"""domain/intake — inbound VCS event router + filters + M05 intake registry.

`POST /api/intake/{type}` is the M05 entry point; the registry maps a
type name to an `IntakeType` handler. Plugins register their own intake
types at bootstrap (e.g. `plugins/github` registers `github_pr` bound to
`pr_review_v1`).
"""

from app.domain.intake import web  # noqa: F401 — registers POST /api/intake/{type}
from app.domain.intake.parsing import is_skippable_path, parse_rereview
from app.domain.intake.registry import (
    IntakePrepared,
    IntakeRejectedError,
    IntakeType,
    _reset_registry_for_tests,
    get_intake_type,
    register_intake_type,
    registered_intake_types,
)
from app.domain.intake.service import (
    IntakeError,
    handle_vcs_events,
    refresh_pr_metadata,
    refresh_pr_metadata_by_id,
)

__all__ = [
    "IntakeError",
    "IntakePrepared",
    "IntakeRejectedError",
    "IntakeType",
    "_reset_registry_for_tests",
    "get_intake_type",
    "handle_vcs_events",
    "is_skippable_path",
    "parse_rereview",
    "refresh_pr_metadata",
    "refresh_pr_metadata_by_id",
    "register_intake_type",
    "registered_intake_types",
]
