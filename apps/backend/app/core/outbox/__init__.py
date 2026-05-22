"""core/outbox — DB-atomic outbound message queue.

The atomic primitive `outbox.write(session, kind, payload)` inserts an
`outbox_entries` row in the caller's session. If the session commits, the
drain (`apps/backend/bin/worker`) dispatches it; if the session rolls back,
nothing happens. See `apps/backend/docs/patterns.md` §
Session management + atomicity.

M05 Phase 0b: model + `write()` primitive. Drain loop scaffolded as a
co-routine that can be wired into the worker entrypoint in Phase 1.
"""

from app.core.outbox.models import OutboxEntryRow
from app.core.outbox.service import drain_once, write

__all__ = ["OutboxEntryRow", "drain_once", "write"]
