"""domain/orgs — orgs, memberships, invitations, SSO config, VCS + coding-agents."""

from app.domain.orgs.coding_agents import (
    CodingAgentAlreadyInstalledError,
    CodingAgentInstall,
    CodingAgentNotInstalledError,
    install_coding_agent,
    list_coding_agents,
    uninstall_coding_agent,
    update_coding_agent_settings,
)
from app.domain.orgs.invitations import (
    InvitationExpiredError,
    InvitationInvalidError,
    InvitationUsedError,
    accept_invitation,
    change_role,
    invite,
    remove_member,
)
from app.domain.orgs.models import (
    InvitationRow,
    MembershipRow,
    OrgCodingAgentRow,
    OrgRow,
    SsoConfigRow,
)
from app.domain.orgs.onboarding import (
    OnboardingStatus,
    get_onboarding_status,
    register_onboarding_contributor,
)
from app.domain.orgs.service import (
    InsufficientRoleError,
    Invitation,
    InvitationError,
    Membership,
    MembershipNotFoundError,
    Org,
    OrgNotFoundError,
    Role,
    SsoConfig,
)
from app.domain.orgs.vcs import (
    VcsState,
    clear_vcs,
    get_vcs,
    set_vcs,
)

# NOTE: `orgs.web` is registered from `main.py` (after `domain.sessions` loads),
# not here — importing it from this __init__ would cycle through
# `domain.sessions.dependencies`, which imports from `domain.orgs`.

__all__ = [
    "CodingAgentAlreadyInstalledError",
    "CodingAgentInstall",
    "CodingAgentNotInstalledError",
    "InsufficientRoleError",
    "Invitation",
    "InvitationError",
    "InvitationExpiredError",
    "InvitationInvalidError",
    "InvitationRow",
    "InvitationUsedError",
    "Membership",
    "MembershipNotFoundError",
    "MembershipRow",
    "OnboardingStatus",
    "Org",
    "OrgCodingAgentRow",
    "OrgNotFoundError",
    "OrgRow",
    "Role",
    "SsoConfig",
    "SsoConfigRow",
    "VcsState",
    "accept_invitation",
    "change_role",
    "clear_vcs",
    "get_onboarding_status",
    "get_vcs",
    "install_coding_agent",
    "invite",
    "list_coding_agents",
    "register_onboarding_contributor",
    "remove_member",
    "set_vcs",
    "uninstall_coding_agent",
    "update_coding_agent_settings",
]
