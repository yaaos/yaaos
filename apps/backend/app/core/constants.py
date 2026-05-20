"""Centralised constants whose values are referenced from multiple modules.

Single source of truth so changes propagate without grep-and-pray. Keep this
file tiny — when a constant belongs to a specific module's contract, prefer
defining it there.
"""

from __future__ import annotations

from datetime import timedelta

# M02 — audit-log retention. The periodic cleanup task in
# `domain/identity/scheduler.py` purges `audit_entries` rows older than this.
AUDIT_LOG_RETENTION = timedelta(days=30)

# M03 — global default idle-session timeout. A session that hasn't been touched
# in this long is treated as expired by the auth dep, regardless of its
# absolute `expires_at`. Orgs can override per-org via `orgs.session_timeout_override`
# (nullable integer minutes) — see `domain/orgs.session_timeout`.
SESSION_IDLE_TIMEOUT = timedelta(hours=12)


__all__ = ["AUDIT_LOG_RETENTION", "SESSION_IDLE_TIMEOUT"]
