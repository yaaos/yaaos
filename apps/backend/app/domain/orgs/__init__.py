"""domain/orgs — orgs, memberships, invitations, SSO config, VCS + coding-agents."""

from app.core.agent_gateway import register_org_arn_lookup as _register_arn_lookup
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
from app.domain.orgs.memberships import list_active_member_ids
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
    SsoConfig,
    create_membership,
    create_org,
    delete_expired_invitations,
    find_saml_org_slug_for_domain,
    get_org,
    get_org_by_slug,
)
from app.domain.orgs.service import _lookup_org_by_arn as _arn_lookup_impl
from app.domain.orgs.sso import (
    SsoConfigError,
    get_config,
    register_assertion_verifier,
    run_assertion_verifier,
    sp_metadata_xml,
    upsert_config,
)
from app.domain.orgs.vcs import (
    VcsClearHook,
    VcsState,
    clear_vcs,
    get_vcs,
    register_vcs_clear_hook,
    set_vcs,
)

_register_arn_lookup(_arn_lookup_impl)

# NOTE: `orgs.web`, `orgs.audit_web`, and `orgs.sso_web` are registered from
# `main.py` (after `core.sessions` loads), not imported here — they cycle
# through `core.sessions.dependencies`. They appear in `__all__` so tach
# allows cross-module side-effect imports.

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
    "SentEmail",
    "SsoConfig",
    "SsoConfigError",
    "VcsClearHook",
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
    "get_org_by_slug",
    "get_test_inbox",
    "get_vcs",
    "install_coding_agent",
    "invite",
    "list_active_member_ids",
    "list_coding_agents",
    "register_assertion_verifier",
    "register_onboarding_contributor",
    "register_vcs_clear_hook",
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
