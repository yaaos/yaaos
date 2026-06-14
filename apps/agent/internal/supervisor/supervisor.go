// Package supervisor implements the long-poll worker loop, identity
// exchange, command routing, heartbeat, and disk-janitor responsibilities
// of the agent's `supervisor` subcommand.
//
// Responsibilities:
//   - identity exchange against the backend's placeholder verifier
//   - N concurrent claim-loop workers (`Config.Concurrency`)
//   - heartbeat loop reporting a workspace registry snapshot
//   - command routing that dispatches each command kind
//   - graceful Stop via context cancellation
package supervisor

import (
	"context"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	oteltrace "go.opentelemetry.io/otel/trace"

	"github.com/yaaos/agent/internal/activity"
	"github.com/yaaos/agent/internal/backoff"
	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/identity"
	"github.com/yaaos/agent/internal/observability"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/tracing"
)

// connection surface tags for the connection.failures / connection.backoff_seconds
// metric attributes. Centralized here so call sites can't typo a label.
const (
	surfaceSTS       = "sts"
	surfaceClaim     = "claim"
	surfaceHeartbeat = "heartbeat"
	surfaceWS        = "ws"
)

// defaultEventPostSteps is the backoff ramp for terminal-event POST retries.
// Shorter than the connection-level ramp: the target is a brief HTTP blip
// (a few seconds), not a multi-minute control-plane outage. Stored on the
// Supervisor as eventPostSteps so tests can inject a fast ramp.
var defaultEventPostSteps = []time.Duration{
	1 * time.Second,
	2 * time.Second,
	5 * time.Second,
	10 * time.Second,
	30 * time.Second,
}

// classifyConnErr returns "auth" if the error carries a 401/403 status
// indication or the literal word "unauthorized", "network" otherwise.
//
// Two error formats come from the protocol client:
//   - doJSON generic path: `"METHOD /path: unauthorized"` (no numeric code)
//   - ClaimCommand: `"claim: unauthorized"` (no numeric code)
//
// Both contain `": unauthorized"` so a suffix check covers them without
// matching unrelated numbers (e.g. port numbers, latency values).
// containsStatus handles the `": <code> "` numeric form; the extra check
// covers the text form.
func classifyConnErr(err error) string {
	if err == nil {
		return ""
	}
	s := err.Error()
	if containsStatus(s, "401") || containsStatus(s, "403") ||
		containsWord(s, "unauthorized") {
		return "auth"
	}
	return "network"
}

func containsStatus(s, code string) bool {
	// Look for `: <code> ` to match the doJSON numeric-status formatting and
	// avoid matching a substring of a port / latency / unrelated number.
	needle := ": " + code + " "
	for i := 0; i+len(needle) <= len(s); i++ {
		if s[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}

func containsWord(s, word string) bool {
	// Look for `: <word>` at the end of the string or followed by space/punct.
	// Avoids matching partial words (e.g. "unauthorized_access" in a path).
	needle := ": " + word
	for i := 0; i+len(needle) <= len(s); i++ {
		if s[i:i+len(needle)] == needle {
			tail := i + len(needle)
			if tail == len(s) || s[tail] == ' ' || s[tail] == '.' || s[tail] == ',' {
				return true
			}
		}
	}
	return false
}

// recordBackoff bumps the connection.failures counter and sets the
// connection.backoff_seconds gauge to the schedule's next delay BEFORE
// calling Sleep. Centralizes the boilerplate so each retry site stays
// readable.
func recordBackoff(ctx context.Context, sched *backoff.Schedule, surface string, err error) {
	class := classifyConnErr(err)
	observability.Metrics().ConnectionFailures.Add(ctx, 1,
		metric.WithAttributes(
			attribute.String("surface", surface),
			attribute.String("class", class),
		),
	)
	observability.Metrics().ConnectionBackoffSeconds.Record(ctx, sched.Peek().Seconds(),
		metric.WithAttributes(attribute.String("surface", surface)),
	)
}

// Config carries the supervisor's runtime knobs.
type Config struct {
	BaseURL           string        // backend root, e.g. "https://yaaos.example.com"
	Version           string        // agent binary version (semver)
	Concurrency       int           // claim-loop workers; defaults to 4
	HeartbeatInterval time.Duration // defaults to 30s
	ClaimWaitSeconds  int           // long-poll horizon per claim; defaults to 30

	// Spawn creates a workspace runner. Defaults to `ExecSpawn(os.Args[0], 5s, log)`
	// — fork+exec of the agent binary's `workspace` subcommand. Tests
	// inject `supervisortest.InProcessSpawn(handler)` (package `internal/supervisor/supervisortest`) so they don't need an OS process.
	Spawn SpawnFunc

	// WorkspaceRoot is the parent directory the workspace process clones
	// repos into. On startup the supervisor scans this for orphan
	// workspaces from a previous run and reports them as
	// `status="unknown"` in the first heartbeat. Empty disables
	// reconciliation; production usually sets it from
	// `YAAOS_WORKSPACE_ROOT`.
	WorkspaceRoot string

	// ActivityWSURL is the backend's activity-stream WebSocket URL
	// (e.g. "wss://yaaos.example.com/api/v1/agent/activity").
	// Agent identity is derived from the bearer — no agent ID in the URL.
	// Empty disables the WS path: progress events fall back to per-event
	// HTTP POSTs. Non-empty: the supervisor dials at startup,
	// constructs an `activity.Conductor`, and routes progress events
	// through it. Demand-pull semantics apply — events for workspaces
	// the backend hasn't sent `subscribe` for are dropped at the
	// Conductor's gate.
	ActivityWSURL string

	// ActivityBatchInterval controls the Conductor's flush cadence.
	// Defaults to 250ms.
	ActivityBatchInterval time.Duration
}

// Logger is the minimal logging surface the supervisor needs. Real
// integrations plug structlog-like loggers in via this interface; tests
// pass a no-op.
type Logger interface {
	Info(msg string, kv ...any)
	Warn(msg string, kv ...any)
	Error(msg string, kv ...any)
}

type nullLogger struct{}

func (nullLogger) Info(string, ...any)  {}
func (nullLogger) Warn(string, ...any)  {}
func (nullLogger) Error(string, ...any) {}

// Supervisor wires the protocol client into the claim/heartbeat loops.
// Construct with New, then Run blocks until ctx is cancelled.
type Supervisor struct {
	cfg      Config
	client   *protocol.Client
	log      Logger
	provider identity.Provider
	agentID  string
	orgID    string
	pool     *Pool

	// config holds the runtime AgentConfig delivered by ConfigUpdateCommand.
	// Nil until the first ConfigUpdate arrives — nil means unconfigured.
	// Lifecycle is derived: config.Load() == nil → unconfigured.
	config atomic.Pointer[command.AgentConfig]

	// conductor + wsConn are non-nil iff cfg.ActivityWSURL is set AND
	// the WS dial succeeded. Progress events route through the
	// Conductor when present; otherwise they fall back to HTTP POSTs.
	// Terminal events always use HTTP — only progress
	// streaming is on the WS.
	conductor *activity.Conductor
	wsConn    *activity.WSConn
	// wsReadLoopDone is closed when the active activity-WS read loop
	// returns. wsReconnectLoop reads from it to decide when to re-dial.
	wsReadLoopDone chan struct{}

	// Per-surface backoff schedules. A misconfigured ARN slowing down
	// STS exchange must not slow heartbeat retries on an unrelated
	// transient blip; each surface owns its own attempt counter.
	stsBackoff       *backoff.Schedule
	claimBackoff     *backoff.Schedule
	heartbeatBackoff *backoff.Schedule
	wsBackoff        *backoff.Schedule
	// eventPostSteps is the backoff ramp for terminal-event POST retries.
	// postTerminalEvent builds a fresh *backoff.Schedule from it on each
	// call: the method runs concurrently on N claim-loop workers, so a
	// shared schedule would let one worker's Reset collapse another's ramp.
	eventPostSteps []time.Duration

	// dedup caches the terminal AgentEvent for each completed command_id.
	// A re-delivered command_id returns the cached event without re-executing.
	// Lost on restart (at-least-once; crash-loss accepted).
	dedup *dedupCache

	// reauthMu serializes concurrent re-authentication attempts. When N claim
	// workers all receive 401 simultaneously, only the goroutine that acquires
	// the lock calls exchangeIdentity; the rest see the updated bearer on
	// their next iteration without burning extra rate-limit quota.
	reauthMu sync.Mutex
}

// New constructs a Supervisor. The client is wired but identity hasn't
// been exchanged yet — call Run.
func New(cfg Config, client *protocol.Client, log Logger, prov identity.Provider) *Supervisor {
	if log == nil {
		log = nullLogger{}
	}
	if prov == nil {
		panic("supervisor.New: provider must not be nil")
	}
	if cfg.Concurrency <= 0 {
		cfg.Concurrency = 4
	}
	if cfg.HeartbeatInterval <= 0 {
		cfg.HeartbeatInterval = 30 * time.Second
	}
	if cfg.ClaimWaitSeconds <= 0 {
		cfg.ClaimWaitSeconds = 30
	}
	if cfg.Spawn == nil {
		cfg.Spawn = ExecSpawn(os.Args[0], 5*time.Second, log)
	}
	if cfg.ActivityBatchInterval <= 0 {
		cfg.ActivityBatchInterval = 250 * time.Millisecond
	}
	// Parse the ops backoff env once; each surface gets its own schedule built
	// from the shared step list (a malformed value WARNs once, not three times).
	opsSteps, opsCustom := opsBackoffSteps()
	return &Supervisor{
		cfg:      cfg,
		client:   client,
		log:      log,
		provider: prov,
		pool:     NewPool(cfg.Spawn, log),
		// stsBackoff: a fresh pod that has never successfully exchanged identity
		// gives up after 1 hour so the container crashes and the orchestrator
		// can restart it (a misconfigured ARN won't fix itself by retrying
		// forever). Once bootstrapped, renewal failures use claimBackoff /
		// heartbeatBackoff which are indefinite — a transient STS blip must
		// not kill a running pod.
		// YAAOS_AGENT_STS_BACKOFF_SECONDS overrides the step list for test
		// stacks that need fast re-auth; unset → the prod ramp.
		stsBackoff:       parseStsBackoffEnv(),
		claimBackoff:     newOpsBackoff(opsSteps, opsCustom),
		heartbeatBackoff: newOpsBackoff(opsSteps, opsCustom),
		wsBackoff:        newOpsBackoff(opsSteps, opsCustom),
		eventPostSteps:   defaultEventPostSteps,
		dedup:            newDedupCache(dedupCacheSize),
	}
}

// Run exchanges identity and starts the claim + heartbeat goroutines.
// Blocks until ctx is cancelled or identity exchange fails fatally.
func (s *Supervisor) Run(ctx context.Context) error {
	// STS bootstrap: retry on the 1m/3m/5m/15m/60m schedule up to a 1h
	// deadline. An unbootstrapped pod that cannot reach the control plane
	// for 1h exits non-zero so the container orchestrator can restart it.
	// The deadline only applies before the first successful exchange — once
	// bootstrapped, renewal failures use the indefinite bearerRefreshLoop.
	var resp *protocol.IdentityExchangeResponse
	for {
		var err error
		resp, err = s.exchangeIdentity(ctx)
		if err == nil {
			s.stsBackoff.Reset()
			break
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		recordBackoff(ctx, s.stsBackoff, surfaceSTS, err)
		s.log.Warn("supervisor.identity_exchange_failed",
			"err", err.Error(),
			"class", classifyConnErr(err),
			"next_sleep_seconds", int(s.stsBackoff.Peek().Seconds()),
		)
		sleepErr := s.stsBackoff.Sleep(ctx)
		if sleepErr == backoff.ErrDeadlineExceeded {
			s.log.Error("supervisor.bootstrap_deadline_exceeded",
				"detail", "identity exchange failed for 1h; exiting so the orchestrator can restart",
			)
			os.Exit(1)
		}
		if sleepErr != nil {
			return sleepErr
		}
	}
	s.agentID = resp.AgentID
	s.orgID = resp.OrgID
	s.client.SetBearer(resp.Bearer)
	// Pin standard observability dimensions: every log, span, and metric
	// record after this point carries org_id + agent_id. These are stable
	// for the process lifetime — each agent instance belongs to exactly one
	// org and is assigned exactly one agent_id on first exchange.
	slog.SetDefault(slog.Default().With("org_id", s.orgID, "agent_id", s.agentID))
	observability.SetStandardDimensions(s.orgID, s.agentID)
	// Store the backend-assigned instance_id as the OTel service.instance.id.
	// Must run before BindExporter (called later on ConfigUpdate) so the
	// late-bind resource carries the correct instance_id correlatable to
	// workspace_agents.instance_id.
	observability.SetInstanceID(resp.InstanceID)
	s.log.Info("supervisor.identity_exchanged", "agent_id", s.agentID, "org_id", s.orgID, "instance_id", resp.InstanceID)

	// Startup reconciliation: any workspace directory left over from a
	// previous run gets pre-loaded into the registry as Orphaned
	// (status="unknown") so the first heartbeat reports it. The backend
	// decides whether to reclaim or signal cleanup via
	// `HeartbeatResponse.forgotten_workspaces` — the disk janitor in
	// `sendHeartbeat` applies that.
	orphans, paths := scanOrphanWorkspaces(s.cfg.WorkspaceRoot, s.log)
	if len(orphans) > 0 {
		for _, o := range orphans {
			s.pool.seedOrphan(o.WorkspaceID, paths[o.WorkspaceID])
		}
		s.log.Info("supervisor.reconciliation_orphans", "count", len(orphans))
	}

	// Activity WebSocket: opt-in via cfg.ActivityWSURL. On dial failure
	// the supervisor keeps running with progress events on HTTP — the
	// WS is an efficiency hop, not a correctness requirement.
	if s.cfg.ActivityWSURL != "" {
		s.setupActivityWS(ctx, resp.Bearer)
	}

	var wg sync.WaitGroup
	for i := 0; i < s.cfg.Concurrency; i++ {
		wg.Add(1)
		go func(workerNum int) {
			defer wg.Done()
			s.claimLoop(ctx, workerNum)
		}(i)
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		s.heartbeatLoop(ctx)
	}()
	wg.Add(1)
	go func() {
		defer wg.Done()
		s.bearerRefreshLoop(ctx, resp.ExpiresAt)
	}()
	wg.Add(1)
	go func() {
		defer wg.Done()
		s.diskSweepLoop(ctx)
	}()
	wg.Wait()
	// Tear down the activity WS first so the read-loop exits before the
	// pool shuts down; otherwise a slow flush could race with ctx cancel.
	if s.conductor != nil {
		s.conductor.Stop()
	}
	if s.wsConn != nil {
		_ = s.wsConn.Close()
	}
	// Reap any still-running workspace subprocesses on shutdown. Each
	// runner gets SIGTERM → grace → SIGKILL via its own Close.
	s.pool.CloseAll(context.Background())

	// Graceful-shutdown "going away" signal — tell the control plane we're
	// exiting cleanly so it can mark the agent offline + fail held workspaces
	// immediately rather than waiting for the liveness sweeper's next tick.
	// Best-effort: errors are logged but never cause a non-zero exit here.
	if err := s.sendGoingAway(); err != nil {
		s.log.Warn("supervisor.deregister_failed", "err", err.Error())
	}
	return nil
}

// sendGoingAway calls DELETE /api/v1/agent/identity with a short deadline.
// The control plane eagerly marks the agent offline and expires its workspaces.
// Called as the last act of a clean shutdown (after all loops have stopped).
func (s *Supervisor) sendGoingAway() error {
	// 5s deadline: if the backend isn't reachable in 5s during shutdown, skip.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return s.client.Deregister(ctx)
}

// setupActivityWS dials the activity-stream WebSocket and wires the
// Conductor. Dial failure is logged but not fatal — the supervisor
// falls back to HTTP-only progress posts. A read-loop transport error
// triggers a reconnect attempt on the WS backoff schedule (1m/3m/5m/...).
func (s *Supervisor) setupActivityWS(ctx context.Context, bearer string) {
	if s.dialAndStartWS(ctx, bearer) {
		s.wsBackoff.Reset()
	}
	// Reconnect goroutine: if the initial dial failed OR the read-loop
	// later exits, sleep on the WS backoff and re-try. Lives for the
	// life of ctx.
	go s.wsReconnectLoop(ctx, bearer)
}

// dialAndStartWS performs one dial attempt + wires the Conductor + spawns
// the read-loop. Returns true on success. Read-loop exit signals
// `s.wsReadLoopDone` (a channel created per-attempt) so the reconnect
// loop can detect it.
func (s *Supervisor) dialAndStartWS(ctx context.Context, bearer string) bool {
	dialCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	url := s.cfg.ActivityWSURL
	_, endDial := tracing.StartSpan(dialCtx, "agent.activity_ws.dial",
		attribute.String("url", url),
	)
	conn, err := activity.Dial(dialCtx, url, bearer)
	endDial(err)
	if err != nil {
		recordBackoff(ctx, s.wsBackoff, surfaceWS, err)
		s.log.Warn("supervisor.activity_ws_dial_failed",
			"url", url,
			"err", err.Error(),
			"class", classifyConnErr(err),
			"next_sleep_seconds", int(s.wsBackoff.Peek().Seconds()),
		)
		return false
	}
	s.wsConn = conn
	s.conductor = activity.NewConductorWithLogger(s.cfg.ActivityBatchInterval, conn.Send, s.log)
	s.conductor.Start(ctx)
	s.wsReadLoopDone = make(chan struct{})
	// Read-loop: ctx cancel unblocks Read. RunInbound returns on the
	// first transport error; we log and let the reconnect loop re-dial.
	go func() {
		defer close(s.wsReadLoopDone)
		if err := activity.RunInbound(ctx, conn, s.conductor); err != nil && ctx.Err() == nil {
			s.log.Warn("supervisor.activity_ws_read_loop_exited", "err", err.Error())
		}
	}()
	s.log.Info("supervisor.activity_ws_connected", "url", url)
	return true
}

// wsReconnectLoop waits for the active read-loop to exit, then re-dials
// the activity WS on the WS backoff schedule. Runs for the life of ctx.
// First call here may also handle the case where the initial dial in
// setupActivityWS failed — wsReadLoopDone is nil then, so we go straight
// to the backoff Sleep + re-dial.
func (s *Supervisor) wsReconnectLoop(ctx context.Context, bearer string) {
	for {
		if s.wsReadLoopDone != nil {
			select {
			case <-ctx.Done():
				return
			case <-s.wsReadLoopDone:
			}
		}
		if ctx.Err() != nil {
			return
		}
		// Sleep before re-dialing. Reset is called inside dialAndStartWS
		// (via the caller in setupActivityWS) on success; here we're
		// post-failure so the schedule advances.
		if err := s.wsBackoff.Sleep(ctx); err != nil {
			return
		}
		if s.dialAndStartWS(ctx, bearer) {
			s.wsBackoff.Reset()
		}
	}
}

func (s *Supervisor) exchangeIdentity(ctx context.Context) (*protocol.IdentityExchangeResponse, error) {
	ctx, end := tracing.StartSpan(ctx, "agent.identity_exchange")
	var err error
	defer func() { end(err) }()

	// The provider signs the claim; the supervisor owns the HTTP exchange.
	// Audience is derived from the backend's BaseURL host so the verifier can
	// reject cross-instance replays. An empty host means BaseURL is misconfigured
	// — the backend requires a non-empty audience and would 401 anyway, so we
	// fail fast here with a descriptive error rather than letting the backend
	// surface an opaque audience_mismatch.
	audience := hostFromURL(s.cfg.BaseURL)
	if override := audienceOverride(); override != "" {
		audience = override
	}
	if audience == "" {
		err = fmt.Errorf("identity: cannot derive audience: BaseURL %q has no host", s.cfg.BaseURL)
		return nil, err
	}
	var payload []byte
	payload, err = s.provider.SignClaim(ctx, audience)
	if err != nil {
		err = fmt.Errorf("identity: sign claim: %w", err)
		return nil, err
	}

	meta := gatherAgentMetadata()
	var resp *protocol.IdentityExchangeResponse
	resp, err = s.client.ExchangeIdentity(ctx, protocol.IdentityExchangeRequest{
		Kind:          s.provider.Kind(),
		AgentVersion:  s.cfg.Version,
		AgentMetadata: meta,
		Payload:       string(payload),
	})
	return resp, err
}

// hostFromURL extracts the host (and optional port) from a URL string.
// Used to derive the audience for the signed STS claim. On a parse error the
// raw input is returned unchanged so the caller still has a non-empty audience.
func hostFromURL(rawURL string) string {
	u, err := url.Parse(rawURL)
	if err != nil {
		return rawURL
	}
	return u.Host
}

// gatherAgentMetadata collects static OS metadata for the identity exchange.
// Fields that cannot be determined are left at their zero values (omitted from
// the wire payload by the json:",omitempty" tags in IdentityExchangeRequest).
//
// Containerized agents (ECS Fargate) report the cgroup memory limit, not the
// host RAM — see internal/supervisor/sysinfo.go.
func gatherAgentMetadata() protocol.AgentMetadata {
	return protocol.AgentMetadata{
		OS:          goOS(),
		CPUCount:    cpuCount(),
		MemoryBytes: memoryBytes(),
	}
}

// refreshResult carries the outcome of one identity-renewal attempt.
type refreshResult struct {
	newBearer string
	expiresAt time.Time
	fatal     bool
}

// runOneRefreshCycle calls exchangeIdentity and validates that the returned
// AgentID, OrgID, and InstanceID match the values pinned at startup. A mismatch
// is an identity-integrity violation — caller must treat fatal=true as a
// process-exit signal rather than a retryable error.
func (s *Supervisor) runOneRefreshCycle(ctx context.Context, currentExpiry time.Time) refreshResult {
	_, end := tracing.StartSpan(ctx, "agent.identity_refresh")
	var spanErr error
	defer func() { end(spanErr) }()

	const retryInterval = 60 * time.Second
	resp, err := s.exchangeIdentity(ctx)
	if err != nil {
		spanErr = err
		s.log.Warn("supervisor.bearer_refresh_failed", "err", err.Error())
		return refreshResult{expiresAt: time.Now().Add(retryInterval)}
	}
	// Identity-integrity check: the backend must return the same agent and org
	// that were pinned on first exchange. A mismatch means something has
	// changed under us (pod re-keyed, org reassigned) — continuing would
	// silently operate under the wrong identity. acceptIdentityChange() is the
	// same seam reauthIfUnauthorized uses: always false in production (mismatch
	// is fatal), true only in agent_test builds so resetStack() can recover.
	if resp.AgentID != s.agentID || resp.OrgID != s.orgID {
		if !acceptIdentityChange() {
			s.log.Error("supervisor.identity_mismatch_on_renewal",
				"pinned_agent_id", s.agentID,
				"pinned_org_id", s.orgID,
				"returned_agent_id", resp.AgentID,
				"returned_org_id", resp.OrgID,
			)
			spanErr = fmt.Errorf("identity mismatch: agent_id or org_id changed on renewal")
			return refreshResult{fatal: true}
		}
		s.applyAcceptedIdentityChange(resp)
	}
	return refreshResult{
		newBearer: resp.Bearer,
		expiresAt: resp.ExpiresAt,
	}
}

// bearerRefreshLoop re-exchanges identity ~5 minutes before the bearer
// expires. Bearers have a 1-hour TTL; the 5-minute lead gives the agent
// several retry attempts before the bearer expires. On exchange failure it
// logs and retries every 60s — the existing bearer remains valid until its
// own expiry. An identity-integrity violation (AgentID/OrgID mismatch) is
// fatal — the process exits.
func (s *Supervisor) bearerRefreshLoop(ctx context.Context, expiresAt time.Time) {
	const refreshLead = 5 * time.Minute
	const retryInterval = 60 * time.Second
	for {
		wait := time.Until(expiresAt.Add(-refreshLead))
		if wait < 0 {
			wait = retryInterval
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		result := s.runOneRefreshCycle(ctx, expiresAt)
		if result.fatal {
			// Identity mismatch is unrecoverable — exit so the orchestrator
			// can restart with a fresh identity exchange.
			os.Exit(1)
		}
		if result.newBearer != "" {
			s.client.SetBearer(result.newBearer)
			expiresAt = result.expiresAt
			s.log.Info("supervisor.bearer_refreshed", "expires_at", expiresAt.Format(time.RFC3339), "org_id", s.orgID, "agent_id", s.agentID)
		} else {
			// Exchange failed (transient) — retry sooner.
			expiresAt = result.expiresAt
		}
	}
}

// diskSweepLoop is the proactive failsafe-5 pass — every 5 minutes it
// walks the workspace root and `RemoveAll`s any directory whose
// `.workspace-id` manifest names an id not in the pool's registry.
// Defence against directories the backend never told us to clean up
// (e.g. agent crashed mid-create before reporting).
func (s *Supervisor) diskSweepLoop(ctx context.Context) {
	const interval = 5 * time.Minute
	if s.cfg.WorkspaceRoot == "" {
		return
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		removed := sweepOrphanWorkspaceDirs(s.cfg.WorkspaceRoot, s.pool.KnownIDs(), s.log)
		if removed > 0 {
			s.log.Info("supervisor.disk_sweep_removed", "count", removed)
		}
	}
}

// claimLoop runs one long-poll worker. On a successful claim it decodes the
// raw bytes into a typed Command, emits a `received` event to cancel the
// lease requeue, and dispatches via routeCommand. On ErrNoCommand (204) it
// re-arms; on ctx cancellation it exits.
func (s *Supervisor) claimLoop(ctx context.Context, workerNum int) {
	for {
		if ctx.Err() != nil {
			return
		}
		_, endClaim := tracing.StartSpan(ctx, "agent.claim")
		raw, err := s.client.ClaimCommand(ctx, s.buildClaimRequest())
		if err == protocol.ErrNoCommand {
			endClaim(nil)
			s.claimBackoff.Reset()
			continue
		}
		// Context canceled by graceful shutdown — the transport-level error
		// (context.Canceled / EOF / "terminated signal received") is the expected
		// outcome of SIGTERM during a long-poll, not a real claim failure.
		if err != nil && ctx.Err() != nil {
			endClaim(nil)
			return
		}
		endClaim(err)
		if err != nil {
			// On 401/403: attempt a fresh identity exchange before backing
			// off. If re-auth succeeds the bearer is updated and we can
			// retry without the full backoff interval.
			if s.reauthIfUnauthorized(ctx, err) {
				s.claimBackoff.Reset()
				continue
			}
			recordBackoff(ctx, s.claimBackoff, surfaceClaim, err)
			s.log.Warn("supervisor.claim_error",
				"worker", workerNum,
				"err", err.Error(),
				"class", classifyConnErr(err),
				"next_sleep_seconds", int(s.claimBackoff.Peek().Seconds()),
			)
			if sleepErr := s.claimBackoff.Sleep(ctx); sleepErr != nil {
				return
			}
			continue
		}
		cmd, decErr := command.Decode(raw)
		if decErr != nil {
			s.log.Warn("supervisor.decode_error", "worker", workerNum, "err", decErr.Error())
			continue
		}
		s.claimBackoff.Reset()
		observability.Metrics().CommandsClaimed.Add(ctx, 1, observability.StandardAttrs())

		// Emit a `received` event to cancel the 30-second lease requeue
		// on the backend. Best-effort: a failure here is logged but does not
		// prevent dispatch — at worst the command gets re-queued to pending
		// and re-delivered (at-least-once).
		s.postReceivedEvent(ctx, cmd.Header())

		s.routeCommand(ctx, cmd)
	}
}

// postReceivedEvent posts a `received` non-terminal event to the backend.
// This cancels the 30-second lease requeue on the command row
// (claimed → delivered). Best-effort: errors are logged but never prevent
// dispatch. ConfigUpdate commands carry no workspace_id and are agent-scoped;
// they still have a command_id that the backend tracks for the lease.
func (s *Supervisor) postReceivedEvent(ctx context.Context, header protocol.CommandHeader) {
	ev := protocol.AgentEvent{
		CommandID:       header.CommandID,
		Kind:            protocol.EventReceived,
		ReportedAt:      time.Now().UTC(),
		Traceparent:     header.Traceparent,
		CompletionToken: header.CompletionToken,
	}
	ack, err := s.client.PostCommandEvent(ctx, header.CommandID, ev)
	if err != nil {
		s.log.Warn("supervisor.received_event_failed",
			"command_id", header.CommandID,
			"err", err.Error(),
		)
		return
	}
	s.log.Info("supervisor.received_event_posted",
		"command_id", header.CommandID,
		"command_event_outcome", ack.Outcome,
	)
}

// heartbeatLoop reports liveness + workspace registry snapshot on the
// configured interval. The reconciliation response (`forgotten_workspaces`)
// drives the disk janitor in sendHeartbeat.
func (s *Supervisor) heartbeatLoop(ctx context.Context) {
	ticker := time.NewTicker(s.cfg.HeartbeatInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.sendHeartbeat(ctx)
		}
	}
}

func (s *Supervisor) sendHeartbeat(ctx context.Context) {
	resp, err := s.client.Heartbeat(ctx, protocol.HeartbeatRequest{
		ReportedAt: time.Now().UTC(),
		Workspaces: s.pool.Snapshot(),
	})
	if err != nil {
		// On 401/403: attempt a fresh identity exchange before backing off.
		// If re-auth succeeds the bearer is updated; skip the backoff sleep.
		if s.reauthIfUnauthorized(ctx, err) {
			s.heartbeatBackoff.Reset()
			return
		}
		recordBackoff(ctx, s.heartbeatBackoff, surfaceHeartbeat, err)
		s.log.Warn("supervisor.heartbeat_error",
			"err", err.Error(),
			"class", classifyConnErr(err),
			"next_sleep_seconds", int(s.heartbeatBackoff.Peek().Seconds()),
		)
		// Sleep absorbs the configured HeartbeatInterval — the ticker
		// will still fire at its cadence, but the next attempt only
		// proceeds after the backoff window elapses.
		_ = s.heartbeatBackoff.Sleep(ctx)
		return
	}
	s.heartbeatBackoff.Reset()
	if len(resp.ForgottenWorkspaces) == 0 {
		return
	}
	s.log.Info("supervisor.heartbeat_reconciled", "forgotten_count", len(resp.ForgottenWorkspaces))

	// Disk janitor: the backend named workspaces it no longer tracks.
	// Walk the pool's path map, `os.RemoveAll` each surviving dir, and
	// drop the cleaned ids from the registry so the next heartbeat
	// doesn't keep reporting them.
	paths := s.pool.Paths()
	surviving := cleanupForgottenWorkspaces(paths, resp.ForgottenWorkspaces, s.log)
	for _, id := range resp.ForgottenWorkspaces {
		if _, stillPresent := surviving[id]; !stillPresent {
			// Successfully removed — drop from registry so it no longer
			// appears in heartbeats.
			s.pool.remove(id)
		}
	}
}

// routeCommand dispatches a Command into either the in-supervisor path
// (AgentCommand, e.g. ConfigUpdate) or the workspace pool (WorkspaceCommand).
// It then forwards the resulting event back to the control plane. The pool
// spawns a workspace subprocess on the first command for a given workspace_id,
// reuses it for subsequent commands, and reaps it after CleanupWorkspace.
//
// Trace propagation: the command header carries the backend's W3C
// traceparent; we extract that into the ctx, start a child
// `supervisor.dispatch.<kind>` span, then rewrite the command's
// traceparent so the workspace subprocess sees our span as its parent.
// The resulting AgentEvent's traceparent likewise carries our span back
// to the backend.
//
// On runner I/O error / context cancel, the pool emits a synthetic
// `completed_failure` event so the workflow-engine on the backend always
// sees an outcome (it never silently hangs waiting for our reply).
func (s *Supervisor) routeCommand(ctx context.Context, cmd command.Command) {
	header := cmd.Header()

	ctx = tracing.ExtractContext(ctx, header.Traceparent)
	spanAttrs := []attribute.KeyValue{
		attribute.String("workspace_id", header.WorkspaceID),
		attribute.String("command_id", header.CommandID),
		attribute.String("kind", string(header.Kind)),
	}
	if header.WorkflowExecutionID != "" {
		spanAttrs = append(spanAttrs, attribute.String("workflow_id", header.WorkflowExecutionID))
	}
	ctx, end := tracing.StartSpan(ctx, "supervisor.dispatch."+string(header.Kind), spanAttrs...)

	// Dedup check: if this command_id already produced a terminal event,
	// replay the cached result without re-dispatching. Records deduped=true
	// on the span so dashboards can distinguish re-delivery from fresh dispatch.
	if cached, hit := s.dedup.lookup(header.CommandID); hit {
		s.log.Info("supervisor.command_deduped", "command_id", header.CommandID)
		observability.Metrics().CommandsDeduped.Add(ctx, 1, observability.StandardAttrs())
		oteltrace.SpanFromContext(ctx).SetAttributes(attribute.Bool("deduped", true))
		postErr := s.postTerminalEvent(ctx, header, cached)
		end(postErr)
		return
	}

	// Rewrite the wire's traceparent so the workspace subprocess + the
	// AgentEvent we'll post back upstream both see this dispatch span as
	// their parent. The original (backend) parent is recorded via the
	// SDK's span linkage — no information lost.
	childTP := tracing.InjectTraceparent(ctx)
	if childTP != "" {
		cmd.SetTraceparent(childTP)
	}

	// Progress-event forwarder: when the activity WS is up, each in-flight
	// progress AgentEvent goes through the Conductor — batched at ~250ms,
	// filtered by the SubscriptionSet (demand-pull: dropped when no UI is
	// watching this workflow). When the WS isn't configured / dial failed,
	// falls back to per-event HTTP POST. Either way, missing progress events
	// aren't a correctness issue — the terminal event still carries
	// the outcome.
	progressForwarder := func(pev protocol.AgentEvent) {
		if s.conductor != nil {
			s.conductor.Publish(header.WorkspaceID, pev)
			return
		}
		if _, perr := s.client.PostCommandEvent(ctx, header.CommandID, pev); perr != nil {
			s.log.Warn("supervisor.progress_post_failed",
				"command_id", header.CommandID, "err", perr.Error())
		}
	}

	var event protocol.AgentEvent
	switch c := cmd.(type) {
	case command.WorkspaceCommand:
		// Lifecycle gate + dispatch live in routeWorkspaceCmd — the single
		// workspace-bound path, shared with the lifecycle tests.
		event = s.routeWorkspaceCmd(ctx, c, progressForwarder)
	case command.AgentCommand:
		// AgentCommands execute in the supervisor itself. Progress events
		// are not expected for the current AgentCommand kinds; the
		// progressForwarder is available but unused here.
		res, err := c.Execute(ctx, s)
		if err != nil {
			event = failureEvent(header, err.Error())
		} else {
			event = protocol.AgentEvent{
				CommandID:       header.CommandID,
				Kind:            protocol.EventCompletedSuccess,
				Outputs:         res.ToWire(),
				ReportedAt:      time.Now().UTC(),
				Traceparent:     childTP,
				CompletionToken: header.CompletionToken,
			}
		}
	default:
		event = failureEvent(header, fmt.Sprintf("unknown command family %T", cmd))
	}

	// The pool's failureEvent / runner-relayed events keep whatever
	// traceparent the dispatcher saw. Make sure the supervisor's span is
	// what the backend correlates against.
	if childTP != "" {
		event.Traceparent = childTP
	}

	// Cache the terminal event before posting so that a re-delivery while
	// the POST is in-flight or after a crash-restart still replays correctly.
	s.dedup.store(header.CommandID, event)

	postErr := s.postTerminalEvent(ctx, header, event)

	// Span carries the dispatch-level error (post-back failure or pool
	// failure if Dispatch returned a completed_failure event).
	result := "success"
	if event.Kind == protocol.EventCompletedFailure || postErr != nil {
		result = "failure"
	}
	observability.Metrics().CommandsCompleted.Add(ctx, 1,
		metric.WithAttributes(
			attribute.String("result", result),
			attribute.String("kind", string(header.Kind)),
		),
		observability.StandardAttrs(),
	)
	if event.Kind == protocol.EventCompletedFailure {
		end(fmt.Errorf("dispatch failure: %s", event.FailureReason))
		return
	}
	end(postErr)
}

// postTerminalEvent posts a terminal AgentEvent to the control plane with
// backoff retry. It stops on any 200 response (success). On 401/403 it
// attempts a fresh identity exchange before retrying so that a bearer expiry
// during a long-running workspace command does not block the terminal event
// post indefinitely. Any other error is retried on a per-call backoff ramp
// (see s.eventPostSteps). Progress events are NOT routed here — they remain
// best-effort single-shot.
func (s *Supervisor) postTerminalEvent(ctx context.Context, header protocol.CommandHeader, event protocol.AgentEvent) error {
	eventPostBackoff := backoff.NewWithSteps(s.eventPostSteps)
	for {
		spanCtx, endPost := tracing.StartSpan(ctx, "agent.event_post",
			attribute.String("command_id", header.CommandID),
			attribute.String("kind", string(header.Kind)),
		)
		ack, err := s.client.PostCommandEvent(ctx, header.CommandID, event)
		if err == nil {
			// 200 — stamp the outcome attribute and log uniformly.
			oteltrace.SpanFromContext(spanCtx).SetAttributes(
				attribute.String("command_event.outcome", ack.Outcome),
			)
			endPost(nil)
			s.log.Info("supervisor.event_posted",
				"command_id", header.CommandID,
				"kind", string(header.Kind),
				"command_event_outcome", ack.Outcome,
			)
			return nil
		}
		// Record the error on this attempt's span before handling auth / retry.
		endPost(err)
		// Auth error: attempt a fresh identity exchange. If re-auth succeeds,
		// reset the backoff and retry immediately with the updated bearer so a
		// DB wipe between command dispatch and terminal-event delivery does not
		// stall the claim worker goroutine for the full backoff window.
		if s.reauthIfUnauthorized(ctx, err) {
			eventPostBackoff.Reset()
			continue
		}
		// Transient error — record a retry metric and wait.
		s.log.Warn("supervisor.event_post_failed",
			"command_id", header.CommandID, "err", err.Error())
		observability.Metrics().EventsPostRetries.Add(ctx, 1,
			metric.WithAttributes(attribute.String("kind", string(header.Kind))),
			observability.StandardAttrs(),
		)
		if sleepErr := eventPostBackoff.Sleep(ctx); sleepErr != nil {
			// Context cancelled (e.g. graceful shutdown) — return the
			// original post error so the span records a failure.
			return err
		}
	}
}

// routeWorkspaceCmd applies the lifecycle gate and dispatches a WorkspaceCommand
// to the pool. It is the single path for all workspace-bound dispatch — both
// the main routeCommand flow and the lifecycle tests call it. onProgress is
// forwarded to Pool.Dispatch (pass nil to drop progress events).
func (s *Supervisor) routeWorkspaceCmd(ctx context.Context, c command.WorkspaceCommand, onProgress func(protocol.AgentEvent)) protocol.AgentEvent {
	cfg := s.config.Load()
	if cfg == nil {
		return failureEvent(c.Header(), "agent unconfigured")
	}
	return s.pool.Dispatch(ctx, c, onProgress, cfg.MaxWorkspaces)
}

// buildClaimRequest constructs the ClaimRequest the claim-loop POSTs to the
// backend. Lifecycle is derived from the config pointer.
//
// Capacity-pull fields:
//   - new_workspaces = max_workspaces − active count (how many new workspaces
//     the agent can accept); 0 when unconfigured.
//   - workspace_ids = idle Active workspaces (Active workspaces with no current
//     command in-flight) awaiting a pending command; empty when unconfigured.
func (s *Supervisor) buildClaimRequest() protocol.ClaimRequest {
	cfg := s.config.Load()
	if cfg == nil {
		return protocol.ClaimRequest{
			WaitSeconds:   s.cfg.ClaimWaitSeconds,
			Lifecycle:     "unconfigured",
			NewWorkspaces: 0,
			WorkspaceIDs:  []string{}, // empty slice serializes as [] not null
		}
	}
	activeIDs := s.pool.ActiveIDs()
	activeCount := len(activeIDs)
	newWorkspaces := cfg.MaxWorkspaces - activeCount
	if newWorkspaces < 0 {
		newWorkspaces = 0
	}
	// workspace_ids = Active workspaces that have no in-flight command
	// (i.e. idle and ready for the next command).
	idleIDs := s.pool.IdleIDs()
	return protocol.ClaimRequest{
		WaitSeconds:   s.cfg.ClaimWaitSeconds,
		Lifecycle:     "configured",
		NewWorkspaces: newWorkspaces,
		WorkspaceIDs:  idleIDs,
	}
}

// ApplyConfig implements command.AgentOps. Stores the config atomically so
// all goroutines see the update on the next read. After the store the agent
// is considered configured (lifecycle = "configured"). Late-binds the OTLP
// exporter when an endpoint is present in the config.
func (s *Supervisor) ApplyConfig(cfg command.AgentConfig) {
	s.config.Store(&cfg)
	s.log.Info("supervisor.agent_configured",
		"max_workspaces", cfg.MaxWorkspaces,
		"otlp_endpoint", cfg.OTLPEndpoint,
		"otlp_dataset", cfg.OTLPDataset,
		"environment", cfg.Environment,
	)
	// Late-bind the OTLP exporter. When OTLPEndpoint is set, installs the
	// exporter into the global OTel providers. No-op when endpoint is empty.
	observability.BindExporter(
		context.Background(),
		cfg.OTLPEndpoint,
		cfg.OTLPToken.Value(),
		cfg.OTLPDataset,
		cfg.Environment,
	)
}
