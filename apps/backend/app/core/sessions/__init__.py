"""core/sessions — FastAPI dependencies that wire `core/auth` middleware into
identity + orgs lookups.

`core/auth` owns the middleware + contextvars (pure, no domain deps);
`core/sessions` owns the route dependency factories (`require(action)`,
`public_route`) that resolve sessions, orgs, memberships.
"""

# Side-effect import: registers /api/auth/* routes.
from app.core.sessions import web
from app.core.sessions.dependencies import (
    current_actor,
    public_route,
    require,
    required_role_for,
)

__all__ = ["current_actor", "public_route", "require", "required_role_for", "web"]
