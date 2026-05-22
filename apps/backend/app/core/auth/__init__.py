"""core/auth — security middleware, contextvars, action enum.

Pure infrastructure. The role-resolving dependency factories
(`require(action)`, `public_route`) live in `domain/sessions` because they
depend on `domain/identity` + `domain/orgs`. The middleware here only
enforces the header check + post-response guard.
"""

from app.core.auth.context import (
    actor_id_var,
    actor_kind_var,
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
from app.core.auth.types import (
    M02_PROTECTED_PREFIXES,
    PUBLIC_PATH_EXACT,
    PUBLIC_PATH_PREFIXES,
    SESSION_IDLE_TIMEOUT,
    Action,
    is_m02_protected_path,
    is_public_path,
)

__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "M02_PROTECTED_PREFIXES",
    "PUBLIC_PATH_EXACT",
    "PUBLIC_PATH_PREFIXES",
    "SESSION_COOKIE_NAME",
    "SESSION_IDLE_TIMEOUT",
    "Action",
    "AuthMiddleware",
    "actor_id_var",
    "actor_kind_var",
    "clear_cookie_attrs",
    "csrf_cookie_attrs",
    "current_actor_kind",
    "current_org_id",
    "current_user_id",
    "is_m02_protected_path",
    "is_public_path",
    "org_context",
    "org_id_var",
    "public_route",
    "require_org_context",
    "route_security_resolved",
    "session_cookie_attrs",
    "user_id_var",
]
