"""core/intake — inbound signal router.

`POST /api/intake/{type}` is the single entry point for external signals.
The registry maps a type name to an `IntakeType` handler; plugins register
their own intake types at bootstrap (e.g. `plugins/github` registers `github`
which routes every GitHub webhook event).

Every handler returns `IntakeSideEffect` — handlers manage their own ticket
creation inside the endpoint's session so ticket inserts, PR back-references,
audit rows, and workflow.start outbox enqueues commit atomically.
"""

from app.core.intake import web  # noqa: F401 — registers POST /api/intake/{type}
from app.core.intake.parsing import (
    is_mid_band_confirm,
    is_skippable_path,
    parse_rereview,
    parse_yaaos_command,
)
from app.core.intake.registry import (
    IntakeOutcome,
    IntakePoint,
    IntakeRejectedError,
    IntakeSideEffect,
    IntakeType,
    get_intake_type,
    list_intake_points,
    register_intake_point,
    register_intake_type,
    registered_intake_types,
    set_intake_for_tests,
)
from app.core.intake.service import IntakeError

__all__ = [
    "IntakeError",
    "IntakeOutcome",
    "IntakePoint",
    "IntakeRejectedError",
    "IntakeSideEffect",
    "IntakeType",
    "get_intake_type",
    "is_mid_band_confirm",
    "is_skippable_path",
    "list_intake_points",
    "parse_rereview",
    "parse_yaaos_command",
    "register_intake_point",
    "register_intake_type",
    "registered_intake_types",
    "set_intake_for_tests",
]
