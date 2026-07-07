"""domain/actions — synchronous deterministic control-plane stage executors.

Plugins (e.g. `plugins/github`) contribute `Action`s at import time via
`register_action`; `ActionStage.action_id` keys into this registry.
`ActionContext` is flattened — it imports `domain/findings` only — so
`pipelines → actions → findings` stays strictly one-way. No tables: a
result persists on `stage_executions.action_result`.
"""

from app.domain.actions.registry import get_action, list_actions, register_action, set_actions_for_tests
from app.domain.actions.types import (
    Action,
    ActionContext,
    ActionError,
    ActionInfo,
    ActionNotFoundError,
    StageVerdict,
)

__all__ = [
    "Action",
    "ActionContext",
    "ActionError",
    "ActionInfo",
    "ActionNotFoundError",
    "StageVerdict",
    "get_action",
    "list_actions",
    "register_action",
    "set_actions_for_tests",
]
