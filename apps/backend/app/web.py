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
from app.core import redis  # noqa: F401
from app.core import observability

from app.core import sse as _core_sse  # noqa: F401 — registers shutdown hook

observability.configure(role="app")

# 3. Webserver registry must exist before any domain module registers routes.
from app.core import webserver  # noqa: E402

# 4. Core modules whose plugins are domain-facing.
from app.core import audit_log, coding_agent, vcs, workspace  # noqa: F401, E402

# 4a. agent_gateway registers `/v1/*` routes.
from app.core import agent_gateway as _core_agent_gateway  # noqa: F401, E402

# 4b. Intake router — pure infrastructure; no domain imports.
from app.core import intake as _core_intake  # noqa: F401, E402

# 4c. Identity + tenancy + auth middleware. Must be imported before
# any domain module that declares `Depends(require(...))` or
# `Depends(public_route)` so the contextvars + middleware classes exist.
from app.core import identity  # noqa: F401, E402
from app.domain import orgs  # noqa: F401, E402
from app.core import auth  # noqa: F401, E402
from app.core import sessions  # noqa: F401, E402

# 5. Domain modules — order: types first (lessons), then leaf domain modules,
#    then domain modules that depend on others.
from app.domain import lessons  # noqa: F401, E402
from app.domain import tickets  # noqa: F401, E402

# 5a-pipelines. Run-engine modules. Types-first: findings/artifacts/repos
# carry no dependencies on the others; pipelines imports findings;
# actions/pr_review import pipelines + findings. attachments before pipelines
# so pipelines can import it for the adoption matcher (Phase 4+).
from app.domain import attachments as _domain_attachments  # noqa: F401, E402
from app.domain import findings as _domain_findings  # noqa: F401, E402
from app.domain import artifacts as _domain_artifacts  # noqa: F401, E402
from app.domain import repos as _domain_repos  # noqa: F401, E402
from app.domain import pipelines as _domain_pipelines  # noqa: F401, E402
from app.domain import actions as _domain_actions  # noqa: F401, E402
from app.domain import pr_review as _domain_pr_review  # noqa: F401, E402

# 5a. Workspace providers registration.
from app.core.workspace import register_workspace_providers  # noqa: E402

register_workspace_providers()

# 5b. Structural run-sink assertion — `core/coding_agent` registers the sink
# at import time (step 4 above). Crash loud here rather than silently dropping
# agent stdout in `record_agent_event` mid-flow.
from app.core.agent_gateway import get_run_sink as _get_run_sink  # noqa: E402

assert _get_run_sink() is not None, "coding-agent run sink must be registered"
from app.domain.integrations import web as _domain_integrations_web  # noqa: F401, E402
import app.domain.mcp_proxy  # noqa: E402 — triggers mcp_proxy web route registration

# 5c. Inbound MCP server — registers /api/mcp-server/* routes via oauth_web.py
#     side-effect import. The FastMCP ASGI sub-app is mounted after create_app().
import app.domain.mcp_server  # noqa: E402
from app.core.workspace import web as _core_workspace_web  # noqa: F401, E402
from app.core.notifications import web as _notifications_web  # noqa: F401, E402
from app.core.sse import web as _core_sse_web  # noqa: F401, E402

# 5b. domain/integrations — must load before its provider plugins so the
# registry exists at the time plugins/linear etc. call register_provider.
from app.domain import integrations as _domain_integrations  # noqa: F401, E402

# 6. Plugins.
from app.plugins import claude_code, codex, github, linear, notion, rwx  # noqa: F401, E402

# GitHub OAuth identity provider lives inside `plugins/github` now —
# `plugins/oauth_github` was deleted. The github plugin's __init__ calls
# both bootstrap() (VCS) and bootstrap_oauth() (identity).
from app.core.config import get_settings  # noqa: E402

# 6b. Test-only providers — env-gated; modules assert on app_mode=="test".
if get_settings().is_test:
    from app.plugins import oauth_test  # noqa: F401
    from app.plugins import saml_test  # noqa: F401

# 7. Test-only: when YAAOS_CODING_AGENT_STUB is set, wrap every registered
#    coding-agent plugin via the `testing/` layer. The testing layer sits above
#    plugins (`core < domain < plugins < testing`) — nothing in production code
#    depends on it. If the testing layer has been stripped from the deployment
#    (per the wheel exclude in pyproject.toml), this import fails loud — stub
#    mode cannot be silently enabled in a stripped production artifact. Settings
#    also refuses to boot if the flag is set in production, so this branch is
#    unreachable in prod regardless of whether the testing tree is present.
if get_settings().yaaos_coding_agent_stub:
    from app.testing.stub_coding_agent import wrap_all_registered_plugins
    from app.testing.stub_workspace import wrap_all_registered_workspace_providers

    wrap_all_registered_plugins()
    wrap_all_registered_workspace_providers()

# 8. Build the FastAPI app.
app = webserver.create_app()

# 8a. Inbound MCP server — mount the FastMCP ASGI sub-app at /api/mcp-server/mcp
#     and the /.well-known/oauth-authorization-server discovery route.
#     Mounted AFTER create_app() so the FastAPI routes registered via RouteSpec
#     (register, authorize, authorize/consent, token) appear in app.routes first
#     and take priority over the sub-app Mount for their specific paths.
#     Direct-mount mirrors the _e2e_setup.mount pattern.
#
#     FastMCP lifespan: Starlette does NOT propagate lifespan events to mounted
#     sub-apps, so we chain the FastMCP http_app's lifespan into the FastAPI
#     router's lifespan_context.  Without this the StreamableHTTPSessionManager's
#     task group never initialises and every /api/mcp-server/mcp request errors.
#
#     StreamableHTTPSessionManager.run() can only be called ONCE per instance.
#     In tests that restart the ASGI lifespan (e.g. multiple TestClient() uses),
#     a fixed module-level _mcp_http_app would fail on the second lifespan start.
#     Solution: an ASGI proxy whose inner handler is swapped to a FRESH http_app
#     on each lifespan start; this gives each startup a virgin session manager.
from contextlib import asynccontextmanager as _asynccontextmanager  # noqa: E402
from typing import Any as _Any  # noqa: E402

from app.domain.mcp_server.oauth_web import well_known_router as _mcp_well_known_router  # noqa: E402
from app.domain.mcp_server.tools import mcp as _mcp_server  # noqa: E402


class _MCPProxy:
    """ASGI proxy for the FastMCP sub-app.

    A fresh `mcp.http_app()` (and thus a fresh `StreamableHTTPSessionManager`)
    is created on each lifespan startup and stored here.  The proxy forwards
    every ASGI call to the current inner handler.  This allows the parent app's
    lifespan to restart (e.g. in tests using `TestClient(app)` multiple times)
    without hitting the "can only be called once per instance" guard on the
    session manager.
    """

    def __init__(self) -> None:
        self._inner: _Any = None

    async def __call__(self, scope: dict, receive: _Any, send: _Any) -> None:
        if self._inner is None:
            raise RuntimeError("_MCPProxy not initialised — lifespan not running")
        await self._inner(scope, receive, send)


_mcp_proxy = _MCPProxy()
_orig_router_lifespan = app.router.lifespan_context


@_asynccontextmanager
async def _combined_lifespan(_app):
    # Fresh instance every time so session manager starts clean.
    mcp_http_app = _mcp_server.http_app(path="/", stateless_http=True)
    _mcp_proxy._inner = mcp_http_app
    try:
        async with _orig_router_lifespan(_app):
            async with mcp_http_app.router.lifespan_context(mcp_http_app):
                yield
    finally:
        _mcp_proxy._inner = None


app.router.lifespan_context = _combined_lifespan

app.include_router(_mcp_well_known_router, prefix="/.well-known", tags=["mcp_server"])
app.mount("/api/mcp-server/mcp", _mcp_proxy)

# 7b. Test-only HTTP surface (`/api/testing/*`) — reset + seed endpoints used by
# the e2e Playwright suite (and ad-hoc local seeding). `mount_testing_endpoints`
# is the production-safety gate (raises RuntimeError if called with prod settings).
# On non-prod: gate confirms env, then e2e_setup.mount registers routes directly
# (core/webserver cannot import app.testing — layering: core < testing). Post-mount
# `assert_no_testing_routes_in_prod` is defense-in-depth — verifies no testing
# route leaked in. Prod wheels exclude the testing/ tree, so any stray import there
# fails loud.
_settings = get_settings()
if _settings.is_non_prod:
    webserver.mount_testing_endpoints(app, _settings)  # gate; would raise if is_production
    from app.testing import e2e_setup as _e2e_setup

    _e2e_setup.mount(app)
webserver.assert_no_testing_routes_in_prod(app, _settings)

if __name__ == "__main__":
    import uvicorn

    # Pass the already-built `app` OBJECT, not the "app.web:app" import string.
    # A string makes uvicorn re-import this module — but it's already running as
    # `__main__`, and `app.web` is a distinct sys.modules entry, so the whole
    # composition root above would execute a SECOND time (every module-level
    # registration double-firing). Serving the object boots the bootstrap once.
    # Trade-off: no uvicorn reload/multi-worker (both need an import string) —
    # the backend runs single-process per container, so neither is used.
    settings = get_settings()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.yaaos_port,
        ws_ping_interval=30,
        ws_ping_timeout=10,
        # Behind Fly's proxy (and Cloudflare): trust X-Forwarded-Proto/-For so
        # request.url.scheme reflects the original https. Without this, scheme
        # is the internal http and code that builds absolute URLs from the
        # request (e.g. the OAuth redirect_uri in core/sessions) emits http://.
        # Only Fly's proxy can reach the container's internal address, so
        # trusting all forwarded IPs is the standard, safe setting on Fly.
        proxy_headers=True,
        forwarded_allow_ips="*",
        # log_config=None: don't let uvicorn run its own dict-config. Our
        # observability.configure() already owns the root logger (structlog
        # ProcessorFormatter + OTel LoggingHandler + secret-scrub + dim filters).
        # uvicorn's default config sets uvicorn.access propagate=False with its
        # own handler — which both skips the OTLP pipe and clobbers the
        # access-log DEBUG-demotion filter. With None, uvicorn.* loggers
        # propagate to root and flow through one pipe.
        log_config=None,
        # Allow active HTTP connections (long polls, SSE streams) to finish
        # on SIGTERM before the process exits.  30 s is generous enough for
        # long-poll endpoints to return and short enough to fit within the
        # kill_timeout budget in fly.production.toml.
        timeout_graceful_shutdown=30,
    )
