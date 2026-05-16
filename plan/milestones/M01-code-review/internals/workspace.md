# `core/workspace` — Internal Architecture

> Provisioned environment where code work happens. Holds the centralized lifecycle (DB-backed) for every workspace yaaof creates. Plugins are dumb actuators; lifecycle policy is here.

## Purpose

`core/workspace` owns:

- The `workspaces` DB table — every workspace from cradle to grave.
- The `WorkspaceSpec`, `Workspace` Protocol, `WorkspaceProvider` Protocol, `WorkspaceInfo`, and supporting value objects.
- The plugin registry.
- The public API: `register_workspace_provider`, `create_workspace`, `with_workspace`, `close_workspace`, `get_workspace`, `force_close_all`.
- The **reaper background task** that enforces wall-clock caps, retries plugin destroys, and escalates `destroy_failed` rows.
- The admin HTTP endpoints (`/api/workspaces/*`).

The Workspace Protocol exposes **operations** (run a coding-agent CLI inside the workspace), not **paths**. Callers ask the workspace to run their CLI; they never see the internal `working_dir` and never spawn subprocesses themselves. `core/workspace` manages the *environment* — provisioning, lifecycle, cleanup — and forwards CLI invocations to the plugin.

## Public interface (`__all__`)

```python
# Types
"WorkspaceID",
"WorkspaceSpec",
"WorkspaceInfo",
"WorkspaceStatus",
"ResourceCaps",
"NetworkPolicy",

# Protocols
"Workspace",
"WorkspaceProvider",

# Functions
"register_workspace_provider",
"create_workspace",
"with_workspace",         # async context manager
"close_workspace",
"get_workspace",
"force_close_all",

# Exceptions
"WorkspaceError",
"WorkspaceProvisionError",
"WorkspaceNotFoundError",
"WorkspaceExpiredError",
"WorkspaceDestroyError",
```

## Types

```python
WorkspaceID = NewType("WorkspaceID", str)   # UUID4 inside

class WorkspaceStatus(StrEnum):
    CREATING = "creating"
    ACTIVE = "active"
    EXPIRED = "expired"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"
    DESTROY_FAILED = "destroy_failed"

class ResourceCaps(BaseModel):
    cpu_count: int = 2
    memory_mb: int = 2048
    wallclock_seconds: int = 600
    disk_mb: int = 10240

class NetworkPolicy(StrEnum):
    DENY_ALL = "deny_all"
    GITHUB_ONLY = "github_only"
    ALLOW_ALL = "allow_all"

class WorkspaceSpec(BaseModel):
    repo: RepoRef                  # from domain/vcs: { plugin_id, external_id }
    sha: str
    branch_name: str | None = None
    org_id: UUID | None = None     # stamped by create_workspace before provision()
    resource_caps: ResourceCaps = Field(default_factory=ResourceCaps)
    network_policy: NetworkPolicy = NetworkPolicy.GITHUB_ONLY

class CodingAgentCliResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
```

`org_id` is required so workspace plugins can request VCS auth (installation tokens) for the right org. Callers pass it to `create_workspace(org_id, spec)`; the lifecycle service stamps it onto `spec` before calling `provider.provision(spec)`.

`ResourceCaps` and `NetworkPolicy` are advisory in M01 — `in_process_workspace` doesn't enforce them (no isolation). M02 `docker_workspace` will. The values exist now so callers can specify them and the interface is stable.

```python
class WorkspaceInfo(BaseModel):
    id: WorkspaceID
    provider_id: str               # "in_process" / "docker" / etc.
    sha: str
    status: WorkspaceStatus
    created_at: datetime
    activated_at: datetime | None
    expires_at: datetime
    destroyed_at: datetime | None
    age_seconds: float             # computed: now() - created_at
```

`WorkspaceInfo` does NOT expose `working_dir`. Internal paths (or whatever passes for "where work happens" in future Docker/K8s plugins) are plugin-private. Consumers ask the workspace to run things — they don't peek.

## `Workspace` Protocol (returned to callers)

```python
class Workspace(Protocol):
    id: WorkspaceID

    async def info(self) -> WorkspaceInfo:
        """Fresh snapshot of this workspace's state. Reads from the workspaces table."""

    async def run_coding_agent_cli(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: int | None = None,
    ) -> CodingAgentCliResult:
        """Run a coding-agent CLI inside the workspace. The workspace decides
        where/how (cwd, container, sandbox). Subprocess timeout + process-group
        kill are the workspace's responsibility, not the caller's.

        Raises WorkspaceExecError if the binary cannot be spawned at all.
        Non-zero exits and timeouts are returned in the result (not raised)."""
```

That's it. No file/search methods, no `working_dir`. Callers (the `claude_code` plugin in M01) hand `argv` + `env` + `stdin` to the workspace; the workspace runs the CLI.

**Targeted method, not generic `exec`.** Each new capability (run tests, install deps, push commits) arrives as a deliberate new method with its own policy. A generic `exec(argv)` would silently broaden as features land; explicit methods communicate intent.

## `WorkspaceProvider` Protocol (plugin contract)

```python
class WorkspaceProvider(Protocol):
    meta: PluginMeta   # id="in_process", type="workspace", display_name="In-Process Workspace", …

    async def provision(self, spec: WorkspaceSpec) -> dict[str, Any]:
        """Provision the environment. Returns plugin_state: an opaque dict that
        core/workspace persists. Must be sufficient for destroy() to clean up.
        Plugin can put anything serializable here:
          - {"working_dir": "/tmp/yaaof-ws-abc"} for in_process
          - {"container_id": "...", "volume_id": "..."} for docker
        Should raise WorkspaceProvisionError on failure (with cause)."""

    async def run_coding_agent_cli(
        self,
        plugin_state: dict[str, Any],
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: int | None = None,
    ) -> CodingAgentCliResult:
        """Run a coding-agent CLI given the plugin_state from provision()."""

    async def destroy(self, plugin_state: dict[str, Any]) -> None:
        """Destroy the workspace given the state from a prior provision().
        MUST be idempotent: if already destroyed, succeed silently.
        Should tolerate partial state (the plugin might have crashed mid-provision
        and core/workspace might have only a partial plugin_state).
        Raises WorkspaceDestroyError on hard failure."""

    async def health_check(self) -> HealthStatus:
        """Cheap check that the provider is reachable + healthy."""
```

`plugin_state` is opaque to the lifecycle layer; consumers never see it. `core/workspace` wraps each persisted state in a `Workspace` Protocol object that forwards `run_coding_agent_cli` to the provider.

## DB lifecycle

Every state transition is a row update on `workspaces`. The function flow:

### `create_workspace(provider_id, spec) → Workspace`

1. Insert row: `status='creating'`, `expires_at=now() + spec.resource_caps.wallclock_seconds`.
2. Call `provider.provision(spec)`.
3. On success: update row → `status='active'`, `activated_at=now()`, `plugin_state=<from provision>`.
4. Return `Workspace` handle.
5. On `WorkspaceProvisionError`: update row → `status='destroy_failed'` with `last_destroy_error`. (Nothing to destroy; mark for ops attention.) Raise.

### `with_workspace(provider_id, spec)` (context manager)

```python
async with with_workspace(provider_id, spec) as ws:
    ... # ws is a Workspace Protocol object
# on exit (normal or exception): close_workspace(ws.id) called automatically
```

`close_workspace(ws_id)` flips status `active → expired` (signals the reaper to destroy on next sweep). It does NOT call `provider.destroy()` synchronously — destruction is the reaper's job. This keeps `close` fast and lets the reaper handle retries uniformly.

### `force_close_all(org_id)`

Flips every workspace in `('creating', 'active')` for the org to `'expired'`. Reaper picks them up on next sweep. Returns count flipped.

### Reaper (background loop)

A plain `async def` loop in `core/workspace`, spawned via `core/primitives.spawn(name="workspace.reaper", coro=_reaper_loop())` from FastAPI's `lifespan`. The loop is `while not shutdown: await _reaper_sweep(); await asyncio.sleep(get_settings().reaper_interval_seconds)`. Interval comes from `YAAOF_REAPER_INTERVAL_SECONDS` (default 30s in prod, 1s in tests). No task scheduler involved.

Per sweep:

1. **Expire over-budget.** `UPDATE workspaces SET status='expired' WHERE status='active' AND expires_at < now() RETURNING id`. Log each expiration.
2. **Destroy expired + closing.** `SELECT * FROM workspaces WHERE status IN ('expired', 'creating') AND destroy_attempts < 3 ORDER BY created_at LIMIT 50` (bounded batch per sweep to avoid one runaway sweep doing too much work).
   - Backoff before retry: `2^destroy_attempts` seconds since `last_destroy_attempt_at`. Attempts 0, 1, 2 → wait 0s, 2s, 4s minimum between attempts.
   - For each row: `UPDATE ... SET status='destroying', destroy_attempts=destroy_attempts+1, last_destroy_attempt_at=now()`. Call `provider.destroy(plugin_state)`. On success: `status='destroyed', destroyed_at=now()`. On failure: `status='expired'` again (so it gets retried), set `last_destroy_error`. After 3 attempts fail: `status='destroy_failed'`, write audit_entry `kind='workspace.destroy_failed'` with full error history, emit loud log.
3. **Stuck-state cleanup.** `UPDATE workspaces SET status='destroy_failed', last_destroy_error='stuck in destroying for > 5 minutes' WHERE status='destroying' AND last_destroy_attempt_at < now() - interval '5 minutes' RETURNING id`. Logged + audited. Means the plugin's destroy hung; manual intervention needed.

The reaper relies on a `last_destroy_attempt_at` column on `workspaces` (see [data-model.md](../data-model.md)) for backoff calculations between retries.

## Bootstrap

At yaaof startup (per the order in `patterns.md`):

1. `core/workspace` initializes its module-level registry.
2. Plugin packages get imported (`plugins/in_process_workspace`); each calls `register_workspace_provider(provider)`.
3. The reaper loop is spawned from FastAPI's `lifespan` via `core/primitives.spawn` (sweeps every 30s until shutdown).
4. **Startup recovery:** before the first reaper sweep, run one pass that picks up workspaces in non-terminal states left from a prior process (`'creating' / 'active' / 'destroying'`) — flip them all to `'expired'` and let the reaper handle destroy. This ensures we don't leak across restarts.

## Admin HTTP endpoints

Owned by `core/workspace`, registered via `register_routes(RouteSpec(module_name="workspace", url_prefix="/api/workspaces", router=router))`. The explicit `url_prefix` overrides the default `/api/workspace` to use the plural form (module name is singular for grammatical reasons; URL is plural per REST convention). No auth (M01 has no auth — these are exposed to anyone on the network; **document as a known POC limitation; tighten when auth lands**).

| Method + path | Purpose |
|---|---|
| `GET /api/workspaces` | List workspaces with filters (status, provider_id, age range). Returns `list[WorkspaceInfo]`. |
| `GET /api/workspaces/{id}` | Get one. |
| `POST /api/workspaces/{id}/close` | Force-close one (sets status to `'expired'`). |
| `POST /api/workspaces/force_close_all` | Force-close every active workspace. Returns count. |
| `POST /api/workspaces/{id}/retry_destroy` | Reset `destroy_failed` → `'expired'` so the reaper retries. For operator response after fixing a plugin bug. |

These are operational endpoints. Not surfaced in the user UI. Reachable via `curl`.

## Audit log writes

| Kind | When | Payload |
|---|---|---|
| `workspace.created` | After `provision()` succeeds | `{workspace_id, provider_id, spec, working_dir, expires_at}` |
| `workspace.closed` | When `close_workspace` flips status | `{workspace_id, reason: 'caller_close' | 'force_close' | 'expired'}` |
| `workspace.destroyed` | When reaper completes destroy | `{workspace_id, age_seconds}` |
| `workspace.destroy_failed` | After 3 failed retries | `{workspace_id, attempts, errors[]}` |
| `workspace.destroy_stuck` | Stuck in `'destroying'` > 5min | `{workspace_id, plugin_id}` |

Entity is the workspace itself (`audit_for_workspace(workspace_id, ...)` — adds a new helper to `core/audit_log`).

## What `core/workspace` does NOT do

- Does not provision anything itself. Always delegates to a plugin.
- Does not spawn subprocesses itself. Forwards `run_coding_agent_cli` to the plugin; the plugin owns subprocess + timeout + process-group semantics.
- Does not enforce `ResourceCaps` or `NetworkPolicy` in M01 — those rely on plugin support (which `in_process_workspace` lacks).
- Does not provide file-read / file-search APIs. CLI agent has its own tools and runs them via `run_coding_agent_cli`.
- Does not retry `provision()` failures — those are usually permanent (missing creds, repo unreachable, etc.). Reaper only retries `destroy()`.

## What it explicitly does in POC mode

- No auth on admin endpoints — anyone on the network can `force_close_all`.
- `in_process_workspace` ignores resource caps and network policy. The agent CLI has the same permissions as yaaof's process.
- Reaper sweeps every 30s; a sweep can take a few seconds if destroys are slow. Acceptable at POC scale (few workspaces in flight at once).

## Forward compatibility: long-lived workspaces (M02+)

M01 destroys a workspace at the end of each agent invocation. **That's an M01 optimization, not a model constraint.** When implementer agents arrive (M02+), workspaces become long-lived environments owned by a ticket: created when implementation begins, surviving multiple implementer ↔ reviewer rounds, destroyed when the ticket completes (PR merged) or is abandoned. Hours to days, not minutes.

The expected future model:

- **A ticket owns its workspace.** `tickets.workspace_id` (nullable) populated when implementation begins. One active workspace per ticket at a time. The ticket aggregate decides lifecycle ("start implementation," "abandon," "complete").
- **Two orthogonal state dimensions.** `workspaces.state` (the column M01 has today) tracks **environmental state** only — `creating / active / expired / destroying / destroyed / destroy_failed`. A separate **workflow state** dimension — `fresh / claimed_by_<agent> / dirty_uncommitted / committed_to_branch / pushed_to_remote / awaiting_review / under_review / awaiting_revision` — is owned by the future workflow orchestrator (`domain/tickets` or a new `domain/workflow` module). The reaper only reads environmental state; the orchestrator only reads workflow state. **Conflating these two would couple the modules.**
- **Claim / release Protocol** on `core/workspace`, added alongside existing methods. Long-lived workspaces are accessed via `claim_workspace(workspace_id, claimant, *, lease_seconds) → Workspace` and `release_workspace(workspace_id, claimant, *, new_workflow_state)`. The lease has a TTL; if the claimant crashes without releasing, the M02+ invocation supervisor's heartbeat watchdog flips the workspace back to idle. `with_workspace()` (M01's create-and-destroy scope) stays — useful for short-lived work like a single review.
- **Agent → agent handoff** is via the workspace working tree (the code itself, on a branch) **plus** structured DB artifacts (`workspace_artifacts` table or similar — keyed by `(ticket_id, workspace_id, invocation_id)`). Implementer commits to a feature branch; reviewer reads from that branch; reviewer findings go into structured artifacts the implementer reads. **The contract between agents is via DB artifacts, not filesystem conventions** (so a new agent doesn't have to scrape `.yaaof/*.json` files). Rich freeform context may live in the workspace fs, but it's a scratchpad, not a contract.
- **Real isolation becomes mandatory** when implementer arrives — it runs tests, builds, package installs. `plugins/docker_workspace` lands no later than M02 implementer work. M01's `in_process_workspace` stays for review (which only reads).
- **Crash survival becomes mandatory.** Losing hours of implementer work to a yaaof restart is unacceptable. The Docker plugin must persist the container (or its volume) across restarts; `provision()` becomes "create or reattach by id," not "always create new." `plugin_state` is already opaque, so this is compatible with the M01 signature.

### Design constraints on M01 code

To keep the M02+ migration mechanical, M01 code MUST respect:

1. **`workspaces.state` stays purely environmental.** Don't add workflow-aware values (`under_review`, `awaiting_revision`) to this enum. When the orchestrator arrives, it adds its own column — it does not extend `workspaces.state`.
2. **Workspaces are self-standing entities, not children of an invocation.** No `workspaces.review_job_id` FK. The review handler keeps the workspace id in local scope (already the case); future code keeps it on the ticket. Bidirectional: no `review_jobs.workspace_id` FK either.
3. **`WorkspaceSpec` stays invocation-agnostic.** No `for_invocation_id` field, no `purpose: "review" | "implementation"`. The spec describes the environment (repo, sha, caps, network policy); who's using it is a separate concern.
4. **`provision()` plugin signature stays open to reattach semantics.** `provision(spec) → (handle, plugin_state)` doesn't need changes today, but plugin authors should treat `plugin_state` as "everything needed to re-find this environment later," not "the in-memory handle." When the Docker plugin lands, `plugin_state = {"container_id": "...", "volume_id": "..."}` and the same `provision()` call may later become "reattach to this container."
5. **Destruction trigger is decoupled from invocation end.** M01's reviewer flips workspace to `expired` and walks away; the reaper destroys async. This pattern survives unchanged — long-lived workspaces just stay `active` longer, then transition to `expired` when the ticket completes/abandons.

These are five "don't"s, no positive M01 work. The future module additions (claim/release, workflow-state column, workspace_artifacts table, docker plugin) all extend the existing model — none of them require rewriting M01 code.

## Decisions

### 2026-05-14 — Lifecycle is centralized in `core/workspace`; plugins are dumb actuators
The `workspaces` table tracks every workspace; the reaper is owned by core; plugins implement only `provision()` + `destroy()`. Lifecycle policy (when to expire, when to retry, when to escalate) lives in one place.
**Why:** plugin bugs leak workspaces silently if lifecycle is plugin-delegated. Centralizing gives one place to audit, alert, and force-close.

### 2026-05-15 — Workspace state has two orthogonal dimensions: environmental (M01) and workflow (M02+)
`workspaces.state` is reserved for environmental state (`creating / active / expired / destroying / destroyed / destroy_failed`) — what `core/workspace` and the reaper care about. **Workflow state** (`fresh / claimed_by_<agent> / committed_to_branch / awaiting_review / ...`) is a separate dimension owned by the future workflow orchestrator (`domain/tickets` or `domain/workflow`), tracked in a separate column added later.
**Why:** the reaper and the orchestrator have unrelated concerns. Conflating them couples two modules. Reviewer/implementer interplay (claim, release, hand off, revise) is workflow logic that has no business being in core/workspace's lifecycle column.

### 2026-05-15 — Long-lived workspaces are an M02+ concept; M01 keeps create-and-destroy per invocation
Each M01 review_job creates its own workspace, destroyed at invocation end. Three reviews of one PR = three workspaces (coordination-free, wasteful). Long-lived workspaces (one per ticket, surviving implementer ↔ reviewer rounds) arrive with implementer agents in M02+ via a claim/release Protocol added alongside `with_workspace`.
**Why:** M01 has no implementer, so cross-invocation state passing has no use case. Building it speculatively would add real complexity (lease watchdog, hand-off artifacts, persistent storage) for zero current value. The design constraints listed in the "Forward compatibility" section above make the future addition mechanical, not a rewrite.

### 2026-05-14 — Each review_job gets its own workspace
Three agents reviewing one PR = three workspaces (one `git clone` per agent). Wasteful but coordination-free.
**Why:** sharing a workspace across agents needs reference counting / barriers. POC simplicity over efficiency. Revisit when measurable.

### 2026-05-16 — Workspace Protocol exposes `run_coding_agent_cli`, not `working_dir` or generic `exec`
The Protocol is operations, not paths. Consumers ask the workspace to run a coding-agent CLI; the workspace decides where/how (`cwd`, container, sandbox). `working_dir` is plugin-private; `WorkspaceInfo` doesn't carry it either.
**Why:** future Docker/K8s workspaces have no "directory on yaaof's host filesystem." Exposing `working_dir` would lock the Protocol to a single implementation. Targeted method (vs generic `exec`) communicates intent and forces M02+ capabilities (run tests, install deps, push commits) to arrive as deliberate new methods, each with its own policy.

### 2026-05-16 — `WorkspaceSpec.org_id` field
Workspace plugins need to request VCS auth (installation tokens) for the right org at provision time. `create_workspace(org_id, spec)` stamps `spec.org_id` before calling `provider.provision(spec)`.

### 2026-05-14 — Reaper retries destroy 3 times with exponential backoff; then `destroy_failed`
3 attempts (delays 0s, 2s, 4s); past that → `destroy_failed` + loud log + audit. Operator investigates manually.
**Why:** transient failures (Docker daemon flap, network blip) recover within a few seconds. Persistent failures should surface fast, not loop forever.

### 2026-05-14 — Admin endpoints exposed without auth in M01
M01 has no auth; admin endpoints are open to anyone on the network. Documented as a known POC limitation; tightened when auth lands.

### 2026-05-14 — Startup recovery: orphaned non-terminal workspaces flipped to `'expired'` on boot
Reaper handles destroy as normal. Survives ungraceful shutdowns of yaaof.
