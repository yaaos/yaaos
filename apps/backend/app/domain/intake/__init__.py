"""domain/intake — inbound signal router.

`POST /api/intake/{type}` is the single entry point for external signals.
The registry maps a type name to an `IntakeType` handler; plugins register
their own intake types at bootstrap (e.g. `plugins/github` registers `github`
which routes every GitHub webhook event).

A handler returns either `IntakePrepared` (the endpoint creates a ticket and
starts a workflow) or `IntakeSideEffect` (the handler already applied its
mutations against the endpoint's session — used for events like PR close,
install lifecycle, or comments on existing tickets).
"""

from app.domain.intake import web  # noqa: F401 — registers POST /api/intake/{type}
from app.domain.intake.parsing import (
    is_mid_band_confirm,
    is_skippable_path,
    parse_rereview,
    parse_yaaos_command,
)
from app.domain.intake.registry import (
    IntakeOutcome,
    IntakePrepared,
    IntakeRejectedError,
    IntakeSideEffect,
    IntakeType,
    get_intake_type,
    register_intake_type,
    registered_intake_types,
)
from app.domain.intake.service import IntakeError

__all__ = [
    "IntakeError",
    "IntakeOutcome",
    "IntakePrepared",
    "IntakeRejectedError",
    "IntakeSideEffect",
    "IntakeType",
    "get_intake_type",
    "is_mid_band_confirm",
    "is_skippable_path",
    "parse_rereview",
    "parse_yaaos_command",
    "register_intake_type",
    "registered_intake_types",
]
