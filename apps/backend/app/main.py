# ruff: noqa: I001
# I001 is disabled file-wide: the bootstrap order in this file is load-bearing
# (see patterns.md § Bootstrap composition order) and conflicts with isort's
# alphabetic grouping.
"""Entry point. Bootstrap order per `patterns.md` § Bootstrap composition order."""

# 1. Load environment.
from app.core import config  # noqa: F401

# 2. Configure core infrastructure.
from app.core import database, observability, primitives  # noqa: F401

observability.configure()

# 3. Events bus must exist before any domain module subscribes.
from app.core import events  # noqa: F401, E402

# 4. Webserver registry must exist before any domain module registers routes.
from app.core import webserver  # noqa: E402

# 5. Core modules whose plugins are domain-facing.
from app.core import audit_log, workspace  # noqa: F401, E402

# 6. Domain modules — order: types first (vcs, memory), then coding_agent
#    (which references vcs + memory types), then leaf domain modules,
#    then domain modules that depend on others.
from app.domain import vcs  # noqa: F401, E402
from app.domain import memory  # noqa: F401, E402
from app.domain import coding_agent  # noqa: F401, E402
from app.domain import pull_requests  # noqa: F401, E402
from app.domain import tickets  # noqa: F401, E402
from app.domain import reviewer  # noqa: F401, E402
from app.domain import intake  # noqa: F401, E402
from app.domain import settings  # noqa: F401, E402

# 7. Plugins.
from app.plugins import in_process_workspace, claude_code, github  # noqa: F401, E402

# 8. Test-only: when YAAOF_CODING_AGENT_STUB is set, wrap every registered
#    coding-agent plugin via the `testing/` layer. The testing layer sits above
#    plugins (`core < domain < plugins < testing`) — nothing in production code
#    depends on it. If the testing layer has been stripped from the deployment
#    (per the wheel exclude in pyproject.toml), this import fails loud — stub
#    mode cannot be silently enabled in a stripped production artifact.
import os  # noqa: E402

if os.environ.get("YAAOF_CODING_AGENT_STUB", "").lower() in {"1", "true", "yes"}:
    from app.testing.stub_coding_agent import wrap_all_registered_plugins
    from app.testing.stub_workspace import wrap_all_registered_workspace_providers

    wrap_all_registered_plugins()
    wrap_all_registered_workspace_providers()

# 9. Build the FastAPI app.
app = webserver.create_app()
