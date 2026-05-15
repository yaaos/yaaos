# ruff: noqa: I001
# I001 is disabled file-wide: the bootstrap order in this file is load-bearing
# (see patterns.md § Bootstrap composition order) and conflicts with isort's
# alphabetic grouping.
"""Entry point. Bootstrap order per `patterns.md` § Bootstrap composition order.

Skeleton state: no domain or plugin modules yet. The import sequence below is
the M01 shape with future imports commented in their correct positions.

`# noqa: E402` is used where Python's "module imports first" convention
conflicts with our load-bearing import order — specifically, `observability`
must be configured before any module that creates spans.
"""

# 1. Load environment (pydantic-settings reads .env + process env at import time).
from app.core import config  # noqa: F401

# 2. Configure core infrastructure.
from app.core import database, observability  # noqa: F401

# Initialize structlog + (optionally) OTel SDK.
observability.configure()

# 3. Initialize the events bus.  (M01: import core.events here before any domain module.)
# from app.core import events

# 4. Webserver registry must exist before any domain module calls register_routes.
from app.core import webserver  # noqa: E402

# 5. Domain modules.  (M01: import each domain module here so it registers its routes
#    and event subscribers at import time.)
# from app.domain import vcs, settings, repos, intake, tickets, pull_requests, memory, reviewer

# 6. Plugin modules.  (M01: import each plugin so it registers with its parent Protocol's registry.)
# from app.plugins import github, claude_code, in_process_workspace

# 7. Construct the FastAPI app. The lifespan body mounts registered routers,
#    runs on_startup hooks, mounts SPA static files (production only), yields,
#    runs on_shutdown hooks, closes the engine.
app = webserver.create_app()

# 8. uvicorn (or equivalent) takes over — `uvicorn app.main:app --host 0.0.0.0 --port 8080`.
