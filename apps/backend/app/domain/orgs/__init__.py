"""domain/orgs — orgs, memberships, invitations, SSO config, VCS + coding-agents."""

from app.core.agent_gateway import register_org_arn_lookup as _register_arn_lookup
from app.domain.orgs.coding_agents import (
    CodingAgentAlreadyInstalledError,
    CodingAgentInstall,
    CodingAgentNotInstalledError,
    install_coding_agent,
    list_coding_agents,
    uninstall_coding_agent,
    update_coding_agent_settings,
)
from app.domain.orgs.email import (
    SentEmail,
    clear_global_inbox,
    read_sent_emails,
    send_plain,
    set_email_inbox_for_tests,
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
from app.domain.orgs.memberships import list_active_member_ids
from app.domain.orgs.onboarding import (
    OnboardingStatus,
    get_onboarding_status,
    register_onboarding_contributor,
)

# Flat re-exports from repository.py
# Rename-on-promote (name collision with service-layer function of same name but different signature):
#   repository.get_org        -> get_org_full  (repository returns OrgFullView; service.get_org returns Org)
#   repository.get_org_by_slug -> get_org_full_by_slug (same reason)
from app.domain.orgs.repository import (
    get_membership,
    hash_token,
    insert_membership,
    insert_org,
    list_memberships_for_org,
    update_role,
)
from app.domain.orgs.repository import (
    get_org as get_org_full,
)
from app.domain.orgs.repository import (
    get_org_by_slug as get_org_full_by_slug,
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

# Flat re-exports from sso.py (ExemptOwnerWithoutTotpError not previously in __all__)
from app.domain.orgs.sso import (
    ExemptOwnerWithoutTotpError,
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

__all__ = [
    "CodingAgentAlreadyInstalledError",
    "CodingAgentInstall",
    "CodingAgentNotInstalledError",
    "ExemptOwnerWithoutTotpError",
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
    "change_role",
    "clear_global_inbox",
    "clear_vcs",
    "create_membership",
    "create_org",
    "delete_expired_invitations",
    "find_saml_org_slug_for_domain",
    "get_config",
    "get_membership",
    "get_onboarding_status",
    "get_org",
    "get_org_by_slug",
    "get_org_full",
    "get_org_full_by_slug",
    "get_vcs",
    "hash_token",
    "insert_membership",
    "insert_org",
    "install_coding_agent",
    "invite",
    "list_active_member_ids",
    "list_coding_agents",
    "list_memberships_for_org",
    "read_sent_emails",
    "register_assertion_verifier",
    "register_onboarding_contributor",
    "register_vcs_clear_hook",
    "remove_member",
    "run_assertion_verifier",
    "send_plain",
    "set_email_inbox_for_tests",
    "set_vcs",
    "sp_metadata_xml",
    "uninstall_coding_agent",
    "update_coding_agent_settings",
    "update_role",
    "upsert_config",
]

# Side-effect imports: load route-registering web submodules at package import
# time so callers need only `import app.domain.orgs`. Not in __all__ (Rule-9).
# These web files import from `app.core.sessions` which is loaded on demand
# here and never creates a cycle (sessions has no dependency on domain.orgs).
import app.domain.orgs.audit_web  # noqa: E402
import app.domain.orgs.coding_agents_web  # noqa: E402
import app.domain.orgs.org_settings_web  # noqa: E402
import app.domain.orgs.sso_web  # noqa: E402
import app.domain.orgs.vcs_web  # noqa: E402
import app.domain.orgs.web  # noqa: E402, F401
