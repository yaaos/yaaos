"""core/auth — security middleware, contextvars, action enum, route taxonomy,
role enum, and role-policy map.

Pure infrastructure. The role-resolving dependency factories
(`require(action)`, `public_route`) live in `core/sessions` because they
depend on `core/identity` + `domain/orgs`. The middleware here enforces
the X-Org-Slug requirement for ORG_SCOPED routes and the default-deny
post-response guard.

Intra-core layer order: core/auth < core/tenancy < core/identity < core/sessions.
"""

from app.core.auth.auth_failure import (
    AuthFailure,
    auth_failure_response,
    register_handler,
)
from app.core.auth.context import (
    actor_id_var,
    actor_kind_var,
    bind_request_structlog_vars,
    current_actor_kind,
    current_org_id,
    current_user_id,
    org_context,
    org_id_var,
    public_route,
    require_org_context,
    route_security_resolved,
    user_id_var,
)
from app.core.auth.cookies import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    clear_cookie_attrs,
    csrf_cookie_attrs,
    session_cookie_attrs,
)
from app.core.auth.middleware import AuthMiddleware
from app.core.auth.rate_limit import AUTH_LIMIT, MUTATE_LIMIT, limiter
from app.core.auth.role_policy import _REQUIRED_ROLE, Role, required_role_for
from app.core.auth.types import (
    ORG_SCOPED_PREFIXES,
    PUBLIC_EXACT,
    PUBLIC_PREFIXES,
    SESSION_IDLE_TIMEOUT,
    USER_SCOPED_EXACT,
    USER_SCOPED_METHOD_EXACT,
    USER_SCOPED_PREFIXES,
    Action,
    RouteSecurity,
    classify_route,
    is_org_scoped_path,
)

__all__ = [
    "AUTH_LIMIT",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "MUTATE_LIMIT",
    "ORG_SCOPED_PREFIXES",
    "PUBLIC_EXACT",
    "PUBLIC_PREFIXES",
    "SESSION_COOKIE_NAME",
    "SESSION_IDLE_TIMEOUT",
    "USER_SCOPED_EXACT",
    "USER_SCOPED_METHOD_EXACT",
    "USER_SCOPED_PREFIXES",
    "_REQUIRED_ROLE",
    "Action",
    "AuthFailure",
    "AuthMiddleware",
    "Role",
    "RouteSecurity",
    "actor_id_var",
    "actor_kind_var",
    "auth_failure_response",
    "bind_request_structlog_vars",
    "classify_route",
    "clear_cookie_attrs",
    "csrf_cookie_attrs",
    "current_actor_kind",
    "current_org_id",
    "current_user_id",
    "is_org_scoped_path",
    "limiter",
    "org_context",
    "org_id_var",
    "public_route",
    "register_handler",
    "require_org_context",
    "required_role_for",
    "route_security_resolved",
    "session_cookie_attrs",
    "user_id_var",
]
