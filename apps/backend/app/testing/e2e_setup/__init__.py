"""testing/e2e_setup — programmatic test-data control surface.

Exposes a small HTTP surface used by the e2e Playwright specs (and ad-hoc
local-dev seeding) to drive yaaos into known states without resorting to a
batch seed script run at container startup.

Routes (all `POST`, all return 404 in prod — gated on `is_non_prod`):
  - `/api/testing/reset` — truncate every table, then re-run the structural
    seed (`ensure_builtin_agents`). After this call: data tables empty; the
    three built-in reviewer agents exist.
  - `/api/testing/seed/credentials_and_install` — populate yaaos with valid
    GitHub + Anthropic credentials and an active installation row pointing
    at fake-github's seeded org. Body: `{"org_login": "acme"}` (optional).
  - `/api/testing/seed/lesson` — insert a single LessonRow. Body:
    `{"repo_external_id", "title", "body"}`.

Layering: this module lives in the testing layer (above plugins, per
`docs/architecture.md`) so it can depend on every domain + plugin model.
It is mounted via `core/webserver.mount_testing_endpoints` only when
`is_non_prod` (`app_mode` is `dev` or `test`); prod wheels exclude the
testing/ tree entirely (see `pyproject.toml`).
"""

from app.testing.e2e_setup.mount import mount
from app.testing.e2e_setup.service import (
    DEFAULT_ORG_ID,
    delete_org,
    delete_user,
    deregister_workspace_agent,
    is_dev_env,
    read_and_clear_email_inbox,
    reset,
    seed_agent,
    seed_bootstrap_owner,
    seed_broken_integration,
    seed_github_install,
    seed_lesson,
    seed_repo_skill,
    seed_user_with_session,
    seed_workspace,
    seed_workspace_agent,
    set_session_last_seen,
    stage_oauth_test_profile,
)

__all__: list[str] = [
    "DEFAULT_ORG_ID",
    "delete_org",
    "delete_user",
    "deregister_workspace_agent",
    "is_dev_env",
    "mount",
    "read_and_clear_email_inbox",
    "reset",
    "seed_agent",
    "seed_bootstrap_owner",
    "seed_broken_integration",
    "seed_github_install",
    "seed_lesson",
    "seed_repo_skill",
    "seed_user_with_session",
    "seed_workspace",
    "seed_workspace_agent",
    "set_session_last_seen",
    "stage_oauth_test_profile",
]
