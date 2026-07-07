"""domain/repos — per-repo protected-code + auto-approve config, and
intake→pipeline trigger bindings.

Repos themselves aren't entities — external ids from the VCS installation.
The accordion list joins `vcs.list_installation_repos` against this
module's config rows; no config row means `unconfigured`, a state not an
error.
"""

from app.domain.repos.service import (
    add_binding,
    evaluate_protected,
    find_bindings,
    get_settings,
    list_due_schedule_bindings,
    list_repo_configs,
    match_protected,
    pipeline_referenced_by_binding,
    put_settings,
    remove_binding,
)
from app.domain.repos.types import (
    DueFire,
    ProtectedMatch,
    ProtectedPathSet,
    RepoConfigSummary,
    RepoSettings,
    RepoSettingsSpec,
    Schedule,
    TriggerBinding,
    TriggerBindingSpec,
)

__all__ = [
    "DueFire",
    "ProtectedMatch",
    "ProtectedPathSet",
    "RepoConfigSummary",
    "RepoSettings",
    "RepoSettingsSpec",
    "Schedule",
    "TriggerBinding",
    "TriggerBindingSpec",
    "add_binding",
    "evaluate_protected",
    "find_bindings",
    "get_settings",
    "list_due_schedule_bindings",
    "list_repo_configs",
    "match_protected",
    "pipeline_referenced_by_binding",
    "put_settings",
    "remove_binding",
]
