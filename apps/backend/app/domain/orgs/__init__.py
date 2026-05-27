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
    delete_expired_invitations,
    get_org,
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
    "InvitationUsedError",
    "Membership",
    "MembershipNotFoundError",
    "OnboardingStatus",
    "Org",
    "OrgNotFoundError",
    "Role",
    "SsoConfig",
    "VcsState",
    "accept_invitation",
    "change_role",
    "clear_vcs",
    "delete_expired_invitations",
    "get_onboarding_status",
    "get_org",
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
