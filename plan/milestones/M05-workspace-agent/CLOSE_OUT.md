# M05 close-out — what shipped, what's deferred

> Captures the state of M05 at the close of the Phase 4 follow-on run. **Not** an "M05 done" doc — the milestone has explicit deferred items that need owner phases or follow-on slices.

## What shipped end-to-end

The full **PR review pipeline** works end-to-end via the M05 workflow engine, tested at the service tier with both empty and non-empty draft findings:

```
intake (POST /api/intake/github_pr) →
  domain/tickets.create (idempotent) →
    engine.start(pr_review_v1, ticket_payload, workspace_provider) →
      CheckShouldReview (admission gate) →
      ProvisionWorkspace (live core/workspace handle) →
      CodeReview (substrate; body returns success) →
      PostFindings (FindingDraft → admit_raw_findings → vcs.post_review → CommentMessage threads) →
      CleanupWorkspace (workspace expired)
```

The `/api/reviewer/rereview` endpoint also drives `pr_review_v1` via `engine.start`, replacing the legacy `schedule_review` call.

### Phase ticks at close

- ✅ **Phase 0a** module-naming hygiene
- ✅ **Phase 0** required-session pattern
- ✅ **Phase 0b** scaffolding
- ✅ **Phase 0c** OTel SDK wiring
- ✅ **Phase 1** core/workflow engine
- ✅ **Phase 2** intake + ticket extensions
- ✅ **Phase 3** core/workspace single-flight claim
- 🟡 **Phase 4** reviewer commands + admission — Reflection ticked; deferred items annotated below
- ✅ **Phase 5** core/agent_gateway + wire protocol
- 🟡 **Phase 6** Go agent — supervisor scaffold shipped; workspace subprocess body deferred
- 🟡 **Phase 7** RemoteAgentWorkspaceProvider — provider + substrate shipped; STS verifier + UI deferred
- 🟡 **Phase 8** span propagation — in-process traceparent threaded; Go-side env propagation deferred
- 🟡 **Phase 8b** Activity streaming — pub/sub + WS + SSE shipped; Go-side subscription + UI deferred
- ✅ **Phase 9** packaging + release (Dockerfile + GHCR + deployment guide)
- 🟡 **Phase 10** docs + audits — docs ticked; close-out audits deferred

## Backend test count

- M05 start (Phase 4 follow-on): 638 tests
- M05 close: 730 tests
- **Net +92 tests** across the Phase 4 follow-on slices

## What's deferred (with owner)

Items listed here have explicit deferral annotations in [PHASES.md](PHASES.md). Each names the work it needs and the phase / follow-on slice that owns it.

### Phase 4 deferrals

1. **5 Workspace reviewer command bodies** (`CodeReview`, `IncrementalReview`, `VerifyFix`, `StaleCheck`, `AnswerQuestion`) — currently default to `Outcome.success()`. The base (`_WorkspaceReviewCommand`) resolves workspace + ticket context; each subclass needs to call the matching `domain/coding_agent.<method>` against the live workspace. Substrate is ready: `core/workspace.get_workspace()`, `WorkspaceTicketContext.pr_id`, `domain/coding_agent.build_invocation`. Blocker: test-side `stub_coding_agent` integration for predictable agent outputs.

2. **`queue.py` file deletion + `review_jobs` table drop** — `schedule_review` has zero production callers; the file remains alive for `cancel_pending`, `list_review_jobs_for_pr`, `metrics_summary`, `startup_recovery` (used by legacy SPA endpoints) and `_run_review_job_inner` (still exercised by 3 legacy tests: `test_secrets_skip_service`, `test_rereview_cancel_service`, `test_mcp_review_pipeline_service`). Full deletion needs: (a) migrate legacy SPA endpoints to read `workflow_executions` instead of `review_jobs`; (b) port secrets-detection + in-flight-cancel + MCP integration tests to the new path; (c) drop `review_jobs` migration.

### Phase 6 deferrals (Go agent)

- Workspace subcommand body — IPC pipes, clone, Claude Code invocation, cleanup
- `os/exec` subprocess spawning + SIGTERM-grace-SIGKILL
- Wall-clock timeout per AgentCommand
- Secret-redaction wrapper
- Startup reconciliation (disk inventory)
- Go OTel SDK + in-memory exporter for tests
- Integration test (fake-backend) for full CreateWorkspace → InvokeClaudeCode → CleanupWorkspace cycle

### Phase 7 deferrals

- Real STS verifier (replays signed STS, extracts ARN, matches registered customer)
- Customer ARN registration UI in Org Settings (backend endpoints shipped; SPA component pending)
- Provisioning policy: least-loaded reachable agent (today picks most-recently-heartbeated)
- Docker-compose E2E with Go agent + fake STS

### Phase 8 deferrals (cross-wire span propagation)

- Supervisor exports `TRACEPARENT` env to workspace process on spawn (rides on Phase 6 subprocess body)
- Workspace process exports same env to Claude Code subprocess
- E2E assertion: one trace ID covers `webhook → ... → terminal outcome` across both providers

### Phase 8b deferrals

- uvicorn ping/pong (`--ws-ping-interval=30 --ws-ping-timeout=10`)
- Go-side `subscribed_workspaces` set + 250ms batching
- WebSocket reconnect → re-derive + re-send subscriptions
- SPA-side activity-stream UI consumer
- ActivityEvent trust-boundary audit (no source content in payloads)

### Phase 10 deferrals (close-out audits)

- `plan/notes/security-posture.md` slim — split shipped → moved to docs vs still-future
- Completeness audit — walk requirements.md row by row, prove each shipped or annotate
- Provider parity — same E2E suite passes against both providers (rides on Phase 6 + Phase 4 follow-on)
- Trace-linkage audit — one trace ID continuous from webhook to PR comment
- Cleanup-failsafes fault-injection (all 7 failsafes)
- Phase 10 Reflection

## Definition-of-done check

Per [START_HERE.md § Definition of done](START_HERE.md):

- ❌ Zero `[ ]` in PHASES.md — 32 unchecked items remain (counted at close); all carry deferral annotations
- ❌ Completeness audit ticked with concrete proof
- ✅ `apps/backend/bin/ci` exits 0 (730 tests)
- ✅ `apps/web/bin/ci` exits 0 (vite + tsc + lint clean)
- 🟡 `apps/agent/bin/ci` — verifies in RWX CI image (Go not on local dev shell — expected)
- ✅ Clean git status

**Honest status**: M05 is **not done** per the strict Definition-of-done. The reviewer pipeline is end-to-end functional for the in_memory provider; the Phase 6 Go workspace subprocess and Phase 7 STS verifier are the largest remaining work fronts that the user/team will need to land.

## What to do next (suggested slicing for the remaining work)

1. **Workspace reviewer body wiring** (1 slice per body, ~5 slices total) — pick the simplest (AnswerQuestion) first, integrate `stub_coding_agent`, prove the chain works end-to-end through coding_agent. Then port the rest.
2. **queue.py final dismantle** (3-4 slices) — port the 3 legacy tests' features to the new path; migrate SPA-facing legacy endpoints; drop file + table.
3. **Phase 6 Go workspace subprocess** — multi-session Go work; arguably its own mini-milestone.
4. **Phase 7 STS verifier + Org Settings UI** — backend + SPA work.
5. **Phase 8 Go traceparent env wiring** — rides on Phase 6.
6. **Phase 10 audits** — write each audit as its own slice; tick as work proves them.

## DECISIONS.md

The two decisions logged at certainty 2/5 during this milestone are in [DECISIONS.md](DECISIONS.md):

1. Image registry: GHCR with semver + latest + sha tagging.
2. Phase 0b migration split: per-phase migrations rather than a single `014_create_all_m05`.

All other decisions reached certainty ≥ 3/5 and proceeded silently.
