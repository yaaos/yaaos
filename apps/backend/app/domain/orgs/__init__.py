"""domain/orgs — orgs, memberships, invitations, SSO config, VCS + coding-agents."""

from app.domain.orgs import repository
from app.domain.orgs.coding_agents import (
    CodingAgentAlreadyInstalledError,
    CodingAgentInstall,
    CodingAgentNotInstalledError,
    install_coding_agent,
    list_coding_agents,
    uninstall_coding_agent,
    update_coding_agent_settings,
)
from app.domain.orgs.email import send_plain
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
    create_membership,
    create_org,
    delete_expired_invitations,
    find_saml_org_slug_for_domain,
    get_org,
)
from app.domain.orgs.sso import (
    SsoConfigError,
    get_config,
    register_assertion_verifier,
    run_assertion_verifier,
    sp_metadata_xml,
    upsert_config,
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
    "SsoConfigError",
    "VcsState",
    "accept_invitation",
    "change_role",
    "clear_vcs",
    "create_membership",
    "create_org",
    "delete_expired_invitations",
    "find_saml_org_slug_for_domain",
    "get_config",
    "get_onboarding_status",
    "get_org",
    "get_vcs",
    "install_coding_agent",
    "invite",
    "list_coding_agents",
    "register_assertion_verifier",
    "register_onboarding_contributor",
    "remove_member",
    "repository",
    "run_assertion_verifier",
    "send_plain",
    "set_vcs",
    "sp_metadata_xml",
    "uninstall_coding_agent",
    "update_coding_agent_settings",
    "upsert_config",
]
