"""plugins/github — GitHub VCSPlugin, OAuth identity provider, and the
`github` intake type that routes every GitHub webhook event into the
domain/intake registry.

The OAuth identity provider (`GitHubOAuthProvider`) lives alongside the
VCS plugin because they share credentials, settings, and the test stack.
"""

from app.plugins.github import web  # noqa: F401 — registers install-state read routes
from app.plugins.github.intake_type import GithubIntakeType
from app.plugins.github.oauth import GitHubOAuthProvider, bootstrap_oauth
from app.plugins.github.service import (
    GitHubPlugin,
    bootstrap,
    get_plugin,
    mark_webhook_processed,
    record_app_install,
    record_webhook_event,
    verify_webhook_signature,
)

__all__ = [
    "GitHubOAuthProvider",
    "GitHubPlugin",
    "GithubIntakeType",
    "bootstrap",
    "bootstrap_oauth",
    "get_plugin",
    "mark_webhook_processed",
    "record_app_install",
    "record_webhook_event",
    "verify_webhook_signature",
]

# Register at import time: the VCS plugin (always) + the OAuth identity
# provider (skips itself when client_id / client_secret are unset) + the
# `github` intake type.
bootstrap()
bootstrap_oauth()

from app.domain.intake import register_intake_type  # noqa: E402

try:
    register_intake_type(GithubIntakeType())
except ValueError:
    # Re-import in the same process (e.g., test reload). The already
    # registered handler is still in place.
    pass
