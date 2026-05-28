"""domain/orgs — orgs, memberships, invitations, SSO config, VCS + coding-agents."""

from app.domain.orgs import (
    repository,
    sso,
)
from app.domain.orgs.coding_agents import (
    CodingAgentAlreadyInstalledError,
    CodingAgentInstall,
    CodingAgentNotInstalledError,
    install_coding_agent,
    list_coding_agents,
    uninstall_coding_agent,
    update_coding_agent_settings,
)
from app.domain.orgs.email import SentEmail, get_test_inbox, send_plain
from app.domain.orgs.invitations import (
    InvitationExpiredError,
    InvitationInvalidError,
    InvitationUsedError,
    accept_invitation,
    change_role,
    invite,
    remove_member,
)
from app.domain.orgs.models import InvitationRow, MembershipRow, OrgRow
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

# NOTE: `orgs.web`, `orgs.audit_web`, and `orgs.sso_web` are registered from
# `main.py` (after `domain.sessions` loads), not imported here — they cycle
# through `domain.sessions.dependencies`, which imports from `domain.orgs`.
# They appear in `__all__` so tach allows cross-module side-effect imports.

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
    "OrgNotFoundError",
    "OrgRow",
    "Role",
    "SentEmail",
    "SsoConfig",
    "SsoConfigError",
    "VcsState",
    "accept_invitation",
    "audit_web",
    "change_role",
    "clear_vcs",
    "create_membership",
    "create_org",
    "delete_expired_invitations",
    "find_saml_org_slug_for_domain",
    "get_config",
    "get_onboarding_status",
    "get_org",
    "get_test_inbox",
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
    "sso",
    "sso_web",
    "uninstall_coding_agent",
    "update_coding_agent_settings",
    "upsert_config",
    "web",
]
