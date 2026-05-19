# Distributed scalability — multi-instance audit

What needs to change in the backend so it runs correctly behind a load balancer with N replicas. Codebase audit captures today's state; "fix shape" notes how to address each.

## Architecture recap

- Backend instances are stateless. N replicas behind an ALB. Any request hits any instance.
- Postgres is the only stateful tier. Managed RDS multi-AZ.
- Cross-instance coordination via Postgres only — `LISTEN/NOTIFY`, advisory locks, `SELECT … FOR UPDATE SKIP LOCKED`. No Redis required for POC.

What this requires of the code:
- No in-process state that affects correctness.
- No in-process timers that drive cross-instance behavior.
- No in-process pub/sub.
- Background sweeps coordinated via DB locks / leases.
- Idempotent startup paths.

## Audit — what's already safe

- Sessions (opaque tokens, hashed in DB).
- Audit log.
- Findings, acknowledgments, comment threads — all per-org DB rows.
- Job dispatch (workspaces pull next job from DB).
- `core/config.get_settings()` `functools.cache` — boot-time, immutable, safe.
- Route registry — populated at boot, read-only afterwards.
- Webhook handlers — stateless HTTP, write to DB, return. (Pending: idempotency table — see below.)

## Audit — what breaks under multi-instance

### Hard blockers (duplicate work / state races)

1. **`startup_recovery` reruns on every instance**
   - Location: `apps/backend/app/domain/reviewer/queue.py:1112-1164`
   - Problem: N instances boot → all N independently re-spawn every `queued` review. Duplicate jobs hit workspaces.
   - Fix shape: advisory-lock the recovery sweep (`pg_advisory_lock('reviewer.startup_recovery')`) so only one instance reruns the sweep, OR make every respawn idempotent (check current status before spawning).

2. **Workspace reaper sweeps concurrently**
   - Location: `apps/backend/app/core/workspace/service.py:327-334`
   - Problem: every instance runs the same 30s sweep loop. Concurrent `CREATING → DESTROYING → DESTROYED` transitions can race; double-destroy attempts.
   - Fix shape: claim sweep work via `SELECT … FOR UPDATE SKIP LOCKED`, or wrap the per-tick sweep in an advisory lock so only one instance reaps per tick.

3. **GitHub catch-up poller runs on every instance**
   - Location: `apps/backend/app/plugins/github/service.py:~176-200`
   - Problem: every instance polls every installation every 10s. N× the GitHub API calls; rate-limit risk; concurrent writes to the same PR rows.
   - Fix shape: advisory-lock per installation, or have one instance hold the "catch-up runner" lease at any time.

### Functional blockers (feature breaks, no corruption)

4. **SSE / event stream is in-process pub/sub**
   - Location: `apps/backend/app/core/events/service.py:1-86`
   - Problem: in-memory `_subscribers` dict. Events published on instance A never reach SSE clients connected to instance B.
   - Fix shape: Postgres `LISTEN/NOTIFY`. Each instance opens one `LISTEN` connection on a shared channel; writers `NOTIFY` with an event id; each instance fans out to its local SSE clients. No new infrastructure.

5. **Review debouncing is an in-process `asyncio.sleep`**
   - Location: `apps/backend/app/domain/reviewer/incremental.py:183-187`
   - Problem: two pushes on different instances spawn two independent debounce timers. Both fire. Potentially two reviews.
   - Fix shape: `scheduled_jobs` table with `(pr_id, fire_at)` + unique constraint on `pr_id` (second push updates the row instead of adding). Workers poll, claim with `FOR UPDATE SKIP LOCKED`, fire, delete.

6. **`cancel_pending` cancels only the local `asyncio.Task`**
   - Location: `apps/backend/app/domain/reviewer/queue.py:293-337`
   - Problem: DB row correctly flips to `cancelled`, but `asyncio.Task.cancel()` reaches only the in-memory `_inflight_tasks` dict on the originating instance. If the review is running elsewhere, the subprocess keeps going until its own timeout.
   - Fix shape: the running review's hot loop polls `reviews.status` (or a `cancel_requested` flag) every few seconds and exits gracefully when set. Truly cross-instance; small CPU cost.

### Soft (collapses with #6)

7. **`_inflight_tasks` dict**
   - Location: `apps/backend/app/domain/reviewer/queue.py:85`
   - Only matters for cancellation. Drops out automatically once #6 lands.

## Additional must-have (not yet audited as missing)

- **Webhook idempotency table**: `processed_webhooks(delivery_id PRIMARY KEY, received_at)` with unique constraint. Insert before processing; `ON CONFLICT DO NOTHING` → drop duplicate.
- **CI multi-instance regression test**: spin up two backends pointed at the same DB and run the integration suite with random routing. Single best guarantee that future code doesn't re-introduce in-process assumptions.

## Estimated fix shape (rough)

| Item | Effort |
|---|---|
| Advisory locks (startup_recovery, reaper, catch-up) | Hours — one `core/lock.py` helper + 3 call sites |
| SSE `LISTEN/NOTIFY` | Half-day — rewrite `core/events/service.py`, NOTIFY in event writers |
| Debouncing → `scheduled_jobs` | Half-day — small table, worker loop similar to reaper |
| Cross-instance cancel via DB flag | Quarter-day — flag, polling, remove `_inflight_tasks` |
| Webhook idempotency | Quarter-day — table + dedupe wrapper |
| Multi-instance integration test | Half-day — docker-compose with 2 backends |

Total: ~2–3 days of focused work. No architecture changes. Same modules, same boundaries, just swapping in-process primitives for DB-backed equivalents.

## What stays out

- Redis caching layer (defer until DB load justifies).
- Read replicas (defer).
- Dedicated worker processes separate from web instances (defer; one process type for now).
- Cross-region replication (defer).
- Distributed tracing across instances (already covered by existing OpenTelemetry setup once instances all export to the same backend).

## Operational checklist when scaling beyond one instance

- ALB health check on `/healthz`.
- Minimum two backend instances (N+1 redundancy).
- Postgres connection pool sized so `instance_count × per_instance_pool ≤ pg_max_connections`.
- Rolling deploys: drain SSE connections gracefully (close with reconnect hint; clients hit another instance).
- Verify each of the seven audit items has been fixed before turning on the second instance.
