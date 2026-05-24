"""plugins/github — GitHub VCSPlugin + OAuth identity provider + webhook receiver.

M04: absorbed the M02 `plugins/oauth_github` plugin. The OAuth identity
provider (`GitHubOAuthProvider`) now lives alongside the VCS plugin since
they share credentials, settings, and the test stack.
"""

from app.plugins.github import web  # noqa: F401 — registers webhook route
from app.plugins.github.intake_type import GithubPrIntakeType
from app.plugins.github.models import (
    GitHubAppInstallationRow,
    GitHubWebhookEventRow,
)
from app.plugins.github.oauth import GitHubOAuthProvider, bootstrap_oauth
from app.plugins.github.service import (
    GitHubPlugin,
    bootstrap,
    get_plugin,
    mark_webhook_processed,
    record_webhook_event,
    verify_webhook_signature,
)

__all__ = [
    "GitHubAppInstallationRow",
    "GitHubOAuthProvider",
    "GitHubPlugin",
    "GitHubWebhookEventRow",
    "GithubPrIntakeType",
    "bootstrap",
    "bootstrap_oauth",
    "get_plugin",
    "mark_webhook_processed",
    "record_webhook_event",
    "verify_webhook_signature",
]

# Register at import time: the VCS plugin (always) + the OAuth identity
# provider (skips itself when client_id / client_secret are unset) + the
# `github_pr` intake type (M05 Phase 2).
bootstrap()
bootstrap_oauth()

from app.domain.intake import register_intake_type  # noqa: E402

try:
    register_intake_type(GithubPrIntakeType())
except ValueError:
    # Re-import in the same process (e.g., test reload). The previously
    # registered handler is still in place.
    pass
