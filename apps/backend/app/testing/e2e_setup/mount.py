"""Direct app-mount helper for the e2e_setup testing surface.

`mount(app)` includes the e2e_setup router at the /api/testing prefix.
Routes are immediately visible in `app.routes` — the composition root
(app/web.py) calls this after `core/webserver.mount_testing_endpoints`
confirms we are in a non-production environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI


def mount(app: FastAPI) -> None:
    """Mount /api/testing/* routes directly on `app`.

    Includes the e2e_setup router at the `/api/testing` prefix so routes are
    immediately visible in `app.routes`. Called by the composition root
    (app/web.py) in non-production environments after the prod-safety gate.
    """
    from app.testing.e2e_setup.web import router  # noqa: PLC0415

    app.include_router(router, prefix="/api/testing", tags=["e2e_setup"])
