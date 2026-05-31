# ruff: noqa: I001
# I001 is disabled file-wide: the bootstrap order in this file is load-bearing
# (see patterns.md § Bootstrap composition order) and conflicts with isort's
# alphabetic grouping.
"""Entry point. Bootstrap order per `patterns.md` § Bootstrap composition order."""

# 1. Load environment.
from app.core import config  # noqa: F401

# 2. Configure core infrastructure.
# Shutdown hooks register at import time; the runtime iterates them in
# reverse registration order. Pin the foundational modules here so
# database shuts down LAST (most depended-on) and redis shuts down before
# database — anything imported later (tasks, sse, agent_gateway)
# registers afterwards and therefore shuts down first.
from app.core import database  # noqa: F401
from app.core import redis
from app.core import observability

redis.bind_pubsub(redis.RedisPubsub())

observability.configure(role="app")

# 3. Webserver registry must exist before any domain module registers routes.
from app.core import webserver  # noqa: E402

# 4. Core modules whose plugins are domain-facing.
from app.core import audit_log, workspace  # noqa: F401, E402

# 4a. workflow engine + agent gateway. Workflow engine registers the
# three taskiq task names at import; agent_gateway registers `/v1/*` routes.
from app.core import workflow as _core_workflow  # noqa: F401, E402
from app.core import agent_gateway as _core_agent_gateway  # noqa: E402

_core_agent_gateway.bind_subscriber_registry(_core_agent_gateway.SubscriberRegistry())

# 4b. Identity + tenancy + auth middleware. Must be imported before
# any domain module that declares `Depends(require(...))` or
# `Depends(public_route)` so the contextvars + middleware classes exist.
from app.core import identity  # noqa: F401, E402
from app.domain import orgs  # noqa: F401, E402
from app.domain.orgs.email import _Inbox, bind_email_inbox  # noqa: E402

bind_email_inbox(_Inbox())
from app.core import auth  # noqa: F401, E402
from app.core import sessions  # noqa: F401, E402

# Register `/api/memberships/*` and `/api/audit/*` after both `domain.orgs`
# and `core.sessions` are loaded — `orgs.web` imports `core.sessions.dependencies`,
# which imports back into `domain.orgs`, so the cycle must break here, not in
# `orgs/__init__`.
from app.core.identity import user_web as _identity_user_web  # noqa: F401, E402
from app.domain.orgs import audit_web as _orgs_audit_web  # noqa: F401, E402
from app.domain.orgs import sso_web as _orgs_sso_web  # noqa: F401, E402
from app.domain.orgs import web as _orgs_web  # noqa: F401, E402

# 5. Domain modules — order: types first (vcs, lessons), then coding_agent
#    (which references vcs + lessons types), then leaf domain modules,
#    then domain modules that depend on others.
from app.domain import vcs  # noqa: F401, E402
from app.domain import lessons  # noqa: F401, E402
from app.domain import coding_agent  # noqa: F401, E402
from app.domain import pull_requests  # noqa: F401, E402
from app.domain import tickets  # noqa: F401, E402
from app.domain import reviewer  # noqa: F401, E402

# 5a. Startup assertions — must run after domain/reviewer import so the
# workflow-context provider and recovery policies are registered by the
# domain module's own bootstrap. These crash the process loudly at startup
# if the wiring is wrong, rather than surfacing as a mid-flow None.
from app.core.workspace import (  # noqa: E402
    assert_workflow_context_provider,
    register_workspace_recovery_policies,
)

register_workspace_recovery_policies()
assert_workflow_context_provider()
from app.domain import intake  # noqa: F401, E402
from app.domain import plugins as _domain_plugins  # noqa: F401, E402
from app.domain.plugins import web as _domain_plugins_web  # noqa: F401, E402
from app.domain.orgs import byok_routes as _orgs_byok_routes  # noqa: F401, E402
from app.domain.integrations import web as _domain_integrations_web  # noqa: F401, E402
from app.domain.mcp_proxy import web as _domain_mcp_proxy_web  # noqa: F401, E402
from app.domain.orgs import coding_agents_web as _orgs_coding_agents_web  # noqa: F401, E402
from app.domain.orgs import org_settings_web as _orgs_org_settings_web  # noqa: F401, E402
from app.core.workspace import web as _core_workspace_web  # noqa: F401, E402
from app.domain.orgs import vcs_web as _orgs_vcs_web  # noqa: F401, E402
from app.core.notifications import web as _notifications_web  # noqa: F401, E402
from app.core.sse import web as _core_sse_web  # noqa: F401, E402

# 5b. domain/integrations — must load before its provider plugins so the
# registry exists at the time plugins/linear etc. call register_provider.
from app.domain import integrations as _domain_integrations  # noqa: F401, E402

# 6. Plugins.
from app.plugins import claude_code, github, linear, notion  # noqa: F401, E402

# GitHub OAuth identity provider lives inside `plugins/github` now —
# `plugins/oauth_github` was deleted. The github plugin's __init__ calls
# both bootstrap() (VCS) and bootstrap_oauth() (identity).
from app.core.config import get_settings  # noqa: E402

# 6b. Test-only providers — env-gated; modules assert on yaaos_env=="test".
if get_settings().yaaos_env == "test":
    from app.plugins import oauth_test  # noqa: F401
    from app.plugins import saml_test  # noqa: F401

# 7. Test-only: when YAAOS_CODING_AGENT_STUB is set, wrap every registered
#    coding-agent plugin via the `testing/` layer. The testing layer sits above
#    plugins (`core < domain < plugins < testing`) — nothing in production code
#    depends on it. If the testing layer has been stripped from the deployment
#    (per the wheel exclude in pyproject.toml), this import fails loud — stub
#    mode cannot be silently enabled in a stripped production artifact.
import os  # noqa: E402

if os.environ.get("YAAOS_CODING_AGENT_STUB", "").lower() in {"1", "true", "yes"}:
    from app.testing.stub_coding_agent import wrap_all_registered_plugins
    from app.testing.stub_workspace import wrap_all_registered_workspace_providers

    wrap_all_registered_plugins()
    wrap_all_registered_workspace_providers()

# 7b. Test-only HTTP surface (`/api/testing/*`) — reset + seed endpoints used by
# the e2e Playwright suite (and ad-hoc local seeding). Mounted only in dev/test
# builds; prod wheels exclude the testing/ tree, so this import would fail loud
# if it ever ran with the layer stripped.
if get_settings().is_non_prod:
    from app.testing import e2e_setup  # noqa: F401

# 8. Build the FastAPI app.
app = webserver.create_app()

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.web:app",
        host="0.0.0.0",
        port=settings.yaaos_port,
        ws_ping_interval=30,
        ws_ping_timeout=10,
    )
