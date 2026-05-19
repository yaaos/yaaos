"""domain/auth — FastAPI dependencies that wire `core/auth` middleware into
identity + orgs lookups.

`core/auth` owns the middleware + contextvars (pure, no domain deps);
`domain/auth` owns the route dependency factories (`require(action)`,
`public_route`) that resolve sessions, orgs, memberships.
"""

from app.domain.auth.dependencies import (
    current_actor,
    public_route,
    require,
    required_role_for,
)

__all__ = ["current_actor", "public_route", "require", "required_role_for"]
