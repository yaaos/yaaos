"""core/intake — placeholder.

All GitHub event handling lives in `plugins/github.intake_type` (the single
`github` IntakeType registered with `core/intake.registry`). This module
exists only to expose `IntakeError` for callers.
"""

from __future__ import annotations


class IntakeError(Exception):
    pass
