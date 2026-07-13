// RealHandler — production command.WorkspaceOps that owns the per-workspace
// tempdir lifecycle. Implements all five workspace command kinds:
//
//   - ProvisionWorkspace   — `os.MkdirTemp` under the configured root +
//                            real `git clone` via the configured
//                            `CloneFunc` (auth injected as
//                            `x-access-token:<token>@…`) + a
//                            `.workspace-id` manifest for startup
//                            reconciliation.
//   - WriteFiles           — write each (path, content) entry under the
//                            workspace root. Refuses paths that escape
//                            the root via `..` or absolute components.
//   - RefreshAuth          — overwrite the stored auth token in-place.
//                            No I/O — used by the supervisor when the
//                            backend rotates a GitHub installation token
//                            mid-flight.
//   - RunClaude            — stats `cmd.SkillPath` inside the checkout
//                            before spawning anything (absent → fail
//                            deterministically with "skill not found:
//                            <path>"); reads `invocation.exec` from the
//                            wire ({argv, stdin, env}), merges env on top
//                            of `os.Environ()`, injects API key secrets from
//                            the last ConfigUpdate (e.g.
//                            ANTHROPIC_API_KEY), sets a workspace-local
//                            TMPDIR (via overrideEnv, so it replaces any
//                            TMPDIR inherited from the agent's own
//                            environment rather than shadowing it), adds
//                            TRACEPARENT for span linkage,
//                            dispatches via the configured `RunFunc`
//                            (production default: `RunStreaming`) with
//                            the workspace tempdir as cwd. After exit —
//                            regardless of claude's own exit status —
//                            reads `$TMPDIR/<command_id>.md` (capped at
//                            `artifactMaxBytes`) and pushes `origin HEAD`
//                            iff HEAD is a named branch (detached PR-ticket
//                            checkouts skip). Captured stdout + the
//                            artifact are returned in the typed
//                            InvokeResult. Zero biz logic — the backend
//                            owns prompt assembly + argv flags + the
//                            skill-path convention.
//   - Cleanup              — `os.RemoveAll` the tempdir + drop the slot.
//                            Idempotent on a missing workspace_id.
//   - PushBranch           — push-failure recovery only: re-points `origin`
//                            at a URL carrying the workspace's *current*
//                            auth token (RefreshWorkspaceAuth only updates
//                            the in-memory slot — the origin URL set at
//                            clone time may hold a stale one) and runs
//                            `git push origin HEAD`. No claude re-run.
//
// Concurrency: a single sync.Mutex serializes slot lookups + mutations.
// Each method is short and non-blocking; the workspace process itself
// dispatches commands single-file via `workspace.Run`, so contention is
// bounded by the supervisor's per-workspace pool serializer.

package workspace

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/secret"
	"github.com/yaaos/agent/internal/tracing"
)

// tokenRedactRe matches `x-access-token:<anything-not-@>@`. Compiled once
// at package init since redaction runs on every git failure path.
var tokenRedactRe = regexp.MustCompile(`x-access-token:[^@]*@`)

// apiKeyProviderEnvVars maps provider_id → environment variable name. When the
// control plane delivers an API key secret for a known provider, RunClaude injects
// it into the subprocess env under this variable. Unknown providers are ignored.
//
// Adding a new provider here requires no other change in the agent.
var apiKeyProviderEnvVars = map[string]string{
	"anthropic": "ANTHROPIC_API_KEY",
	"openai":    "CODEX_API_KEY",
	"rwx":       "RWX_ACCESS_TOKEN",
}

// Excerpt caps for the failure string emitted on non-zero subprocess exit.
// Stderr stays small; stdout gets a larger tail because coding agents with
// structured output (e.g. claude --output-format=stream-json) emit result
// events at the end of stdout, not stderr.
const (
	errStderrCap     = 2048
	errStdoutTailCap = 4096
)

// artifactMaxBytes caps the artifact file RunClaude reads from
// `$TMPDIR/<command_id>.md`. A file over this cap ships no body — the
// artifact_error field explains why so the backend can distinguish "wrote
// none" from "wrote too much".
const artifactMaxBytes = 2 * 1024 * 1024

// workspaceTmpDirName is the workspace-relative TMPDIR RunClaude sets on
// every claude subprocess. Workspace-local so artifact (and any other
// scratch) files are torn down automatically by Cleanup's `os.RemoveAll` —
// no per-file deletion, and files remain for debugging until then.
const workspaceTmpDirName = ".yaaos-invoke-tmp"

// excerptHead returns the first `limit` bytes of `b`, suffixed with a
// truncation marker when bytes were cut. Returns "<empty>" for an empty input
// so the reader can distinguish "captured nothing" from "field missing".
func excerptHead(b []byte, limit int) string {
	if len(b) == 0 {
		return "<empty>"
	}
	if len(b) <= limit {
		return string(b)
	}
	return string(b[:limit]) + "...[truncated tail]"
}

// excerptTail returns the last `limit` bytes of `b`, prefixed with a
// truncation marker when bytes were cut. Used for stdout where the meaningful
// diagnostic (claude's `result` stream-json event) lands at the end. Returns
// "<empty>" for an empty input.
func excerptTail(b []byte, limit int) string {
	if len(b) == 0 {
		return "<empty>"
	}
	if len(b) <= limit {
		return string(b)
	}
	return "...[truncated head]" + string(b[len(b)-limit:])
}

// overrideEnv returns env with every existing "key=..." entry removed and a
// single "key=val" entry appended. Unlike a bare append, this guarantees
// exactly one entry for key regardless of how many times it already appears
// in env (e.g. inherited from os.Environ() plus a wire-supplied override) —
// callers that read the first match (as some libc getenv-style consumers do)
// and callers that read the last match both see the same value. Order of
// unrelated entries is preserved.
func overrideEnv(env []string, key, val string) []string {
	prefix := key + "="
	out := make([]string, 0, len(env)+1)
	for _, kv := range env {
		if strings.HasPrefix(kv, prefix) {
			continue
		}
		out = append(out, kv)
	}
	return append(out, prefix+val)
}

// stripEnv returns env with every "key=..." entry removed. Order of unrelated
// entries is preserved.
func stripEnv(env []string, key string) []string {
	prefix := key + "="
	out := make([]string, 0, len(env))
	for _, kv := range env {
		if strings.HasPrefix(kv, prefix) {
			continue
		}
		out = append(out, kv)
	}
	return out
}

// RealHandlerConfig customizes the production handler's behaviour. Zero
// values pick safe defaults.
type RealHandlerConfig struct {
	// Root is the parent directory that holds per-workspace tempdirs.
	// Empty string means `os.TempDir()`. Production deployments mount a
	// dedicated EBS volume here so disk failures don't take the agent
	// process down.
	Root string

	// DirPerm is the permission bits applied to the per-workspace
	// tempdir + every directory we mkdir inside it. Defaults to 0o700 —
	// the workspace contents are customer source code; no other Linux
	// user on the host can read them.
	DirPerm os.FileMode

	// FilePerm is the permission bits applied to files written via
	// WriteFiles. Defaults to 0o600.
	FilePerm os.FileMode

	// CloneFunc clones a repo into the workspace directory. Defaults to
	// `gitClone`, which shells out to `git` (required in the runtime
	// image; see Dockerfile). Tests inject a no-op or a local-
	// bare-repo clone so they don't touch the network.
	CloneFunc CloneFunc

	// RunFunc spawns the Claude Code subprocess. Defaults to
	// `RunStreaming`; production default is a real child process. Tests
	// inject a fake so they don't spawn a real Claude binary.
	RunFunc RunFunc

	// ApiKeys returns the current per-org API key secret map
	// (provider_id → secret.Secret) as last delivered by the control
	// plane via ConfigUpdateCommand. Nil return means no keys available.
	// Production wires this to a closure over the supervisor's atomic
	// config pointer. Tests inject a fixed map.
	ApiKeys func() map[string]secret.Secret
}

// CloneFunc clones `repo` into `dest`. `auth` carries the credential
// kind + token; production uses `github_installation` tokens injected
// into the clone URL via HTTPS basic auth. `history` is the shallow-
// clone depth (`--depth <history>`); pass 0 to skip the flag.
type CloneFunc func(ctx context.Context, dest string, repo protocol.RepoRef, auth protocol.AuthBlock, history int) error

// RunFunc spawns the Claude Code subprocess (or any streaming child).
// Production default is `RunStreaming`; tests inject a fake so they don't
// spawn a real Claude binary.
type RunFunc func(ctx context.Context, opts RunStreamingOptions) (*RunStreamingResult, error)

// realSlot tracks one workspace's state across the command sequence.
//
// `authTok` is a redacted-by-default `secret.Secret`. Every default
// `fmt` / `log` / `json.Marshal` path involving this struct emits the
// `[REDACTED]` placeholder; only explicit `.Value()` unwraps reveal the
// token bytes. Greppable via `grep -rn '\.Value()' apps/agent`.
type realSlot struct {
	path     string // absolute filesystem path of the workspace tempdir
	repo     protocol.RepoRef
	authKind string        // "github_installation" | "oauth"
	authTok  secret.Secret // never logged in cleartext
}

// RealHandler implements command.WorkspaceOps for production. Construct
// with NewRealHandler.
type RealHandler struct {
	cfg   RealHandlerConfig
	mu    sync.Mutex
	slots map[string]*realSlot
}

// NewRealHandler returns a fresh handler with the given config. Use
// `workspace.Run(ctx, in, out, NewRealHandler(...), opts)` from the
// `agent workspace` subcommand entry point. RealHandler implements
// command.WorkspaceOps.
func NewRealHandler(cfg RealHandlerConfig) *RealHandler {
	if cfg.DirPerm == 0 {
		cfg.DirPerm = 0o700
	}
	if cfg.FilePerm == 0 {
		cfg.FilePerm = 0o600
	}
	if cfg.CloneFunc == nil {
		cfg.CloneFunc = gitClone
	}
	if cfg.RunFunc == nil {
		cfg.RunFunc = RunStreaming
	}
	return &RealHandler{cfg: cfg, slots: make(map[string]*realSlot)}
}

// ErrUnknownWorkspace is returned by WriteFiles / RefreshWorkspaceAuth /
// InvokeClaudeCode when no ProvisionWorkspace has run for the given
// workspace_id. The supervisor surfaces this as a completed_failure
// event; the backend's run engine treats it as a fatal stage error.
var ErrUnknownWorkspace = errors.New("workspace not provisioned")

func (h *RealHandler) ProvisionWorkspace(ctx context.Context, cmd *protocol.ProvisionWorkspaceCommand) (command.ProvisionResult, error) {
	h.mu.Lock()
	if existing, exists := h.slots[cmd.WorkspaceID]; exists {
		// Idempotent: a second ProvisionWorkspace for the same id is a
		// supervisor-side bug, but we don't want to crash the workspace
		// process. Keep the existing slot, report reused=true.
		path := existing.path
		h.mu.Unlock()
		return command.ProvisionResult{
			WorkspaceID: cmd.WorkspaceID,
			Path:        path,
			Reused:      true,
		}, nil
	}
	h.mu.Unlock()

	root := h.cfg.Root
	if root == "" {
		root = os.TempDir()
	}
	path, err := os.MkdirTemp(root, "yaaos-ws-"+sanitizeID(cmd.WorkspaceID)+"-")
	if err != nil {
		return command.ProvisionResult{}, fmt.Errorf("mkdir tempdir: %w", err)
	}
	if err := os.Chmod(path, h.cfg.DirPerm); err != nil {
		// Best-effort: the tempdir already exists with default perms.
		// Don't fail the command on chmod.
		_ = err
	}

	// Clone outside the mutex so concurrent ProvisionWorkspace calls for
	// different workspace_ids don't serialize on the slot map. The
	// tempdir is empty at this point (manifest write happens *after*
	// the clone) — `git clone` refuses non-empty destinations.
	_, endClone := tracing.StartSpan(ctx, "workspace.clone")
	cloneErr := h.cfg.CloneFunc(ctx, path, cmd.Repo, cmd.Auth, cmd.History)
	endClone(cloneErr)
	if cloneErr != nil {
		// Tear down the empty tempdir on clone failure so we don't leak.
		_ = os.RemoveAll(path)
		return command.ProvisionResult{}, fmt.Errorf("git clone: %w", cloneErr)
	}

	// Commit identity: backend-supplied constants, not agent policy. Every
	// skill commit on a named work branch needs `user.name`/`user.email`
	// set — detached-checkout review flows never commit, so this is a
	// harmless no-op for them. Best-effort on an empty pair (older wire
	// payloads / test fixtures that don't set these fields).
	if err := configureGitIdentity(ctx, path, cmd.GitUserName, cmd.GitUserEmail); err != nil {
		_ = os.RemoveAll(path)
		return command.ProvisionResult{}, fmt.Errorf("git identity: %w", err)
	}

	// Startup-reconciliation manifest: write the workspace_id to a
	// well-known file inside the tempdir so the supervisor can find +
	// reattribute orphans when it restarts mid-flight (see
	// supervisor.scanOrphanWorkspaces). The file content is the
	// workspace_id verbatim; no header / encoding to keep it
	// language-agnostic. Written AFTER clone so the clone target stays
	// empty.
	manifestPath := filepath.Join(path, ".workspace-id")
	if err := os.WriteFile(manifestPath, []byte(cmd.WorkspaceID), 0o600); err != nil {
		// Manifest is best-effort; ProvisionWorkspace shouldn't fail
		// because of it. An orphan workspace without manifest is
		// merely invisible to reconciliation, not broken.
		_ = err
	}

	h.mu.Lock()
	defer h.mu.Unlock()
	// Re-check: another goroutine may have raced us in the meantime.
	if existing, raced := h.slots[cmd.WorkspaceID]; raced {
		_ = os.RemoveAll(path)
		return command.ProvisionResult{
			WorkspaceID: cmd.WorkspaceID,
			Path:        existing.path,
			Reused:      true,
		}, nil
	}
	h.slots[cmd.WorkspaceID] = &realSlot{
		path:     path,
		repo:     cmd.Repo,
		authKind: cmd.Auth.Kind,
		authTok:  secret.New(cmd.Auth.Token),
	}
	return command.ProvisionResult{
		WorkspaceID: cmd.WorkspaceID,
		Path:        path,
		Repo:        cmd.Repo.ExternalID,
		HeadSHA:     cmd.Repo.HeadSHA,
		Branch:      cmd.Repo.BranchName,
		Reused:      false,
	}, nil
}

func (h *RealHandler) WriteFiles(_ context.Context, cmd *protocol.WriteFilesCommand) (command.WriteFilesResult, error) {
	h.mu.Lock()
	slot, ok := h.slots[cmd.WorkspaceID]
	h.mu.Unlock()
	if !ok {
		return command.WriteFilesResult{}, ErrUnknownWorkspace
	}
	written := 0
	for _, entry := range cmd.Files {
		full, err := safeJoin(slot.path, entry.Path)
		if err != nil {
			return command.WriteFilesResult{}, fmt.Errorf("file %q: %w", entry.Path, err)
		}
		if err := os.MkdirAll(filepath.Dir(full), h.cfg.DirPerm); err != nil {
			return command.WriteFilesResult{}, fmt.Errorf("file %q: mkdir parent: %w", entry.Path, err)
		}
		if err := os.WriteFile(full, []byte(entry.Content), h.cfg.FilePerm); err != nil {
			return command.WriteFilesResult{}, fmt.Errorf("file %q: write: %w", entry.Path, err)
		}
		written++
	}
	return command.WriteFilesResult{
		WorkspaceID: cmd.WorkspaceID,
		FilesCount:  written,
	}, nil
}

func (h *RealHandler) RefreshAuth(_ context.Context, cmd *protocol.RefreshWorkspaceAuthCommand) (command.RefreshResult, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	slot, ok := h.slots[cmd.WorkspaceID]
	if !ok {
		return command.RefreshResult{}, ErrUnknownWorkspace
	}
	slot.authTok = secret.New(cmd.NewToken)
	return command.RefreshResult{
		WorkspaceID: cmd.WorkspaceID,
		Refreshed:   true,
	}, nil
}

// invocationExec is the wire shape under `cmd.Invocation.exec` —
// produced by `domain/coding_agent.build_invocation`. The
// rest of `cmd.Invocation` (mode, context, prompt_config) is
// observability/contract surface that the agent ignores by design — the
// "zero biz logic" rule means the backend owns prompt assembly.
type invocationExec struct {
	Exec struct {
		Argv  []string          `json:"argv"`
		Stdin string            `json:"stdin"`
		Env   map[string]string `json:"env"`
	} `json:"exec"`
}

func (h *RealHandler) RunClaude(ctx context.Context, cmd *protocol.InvokeClaudeCodeCommand) (command.InvokeResult, error) {
	h.mu.Lock()
	slot, ok := h.slots[cmd.WorkspaceID]
	h.mu.Unlock()
	if !ok {
		return command.InvokeResult{}, ErrUnknownWorkspace
	}

	var inv invocationExec
	if err := json.Unmarshal(cmd.Invocation, &inv); err != nil {
		return command.InvokeResult{}, fmt.Errorf("decode invocation: %w", err)
	}
	if len(inv.Exec.Argv) == 0 {
		return command.InvokeResult{}, errors.New("invocation.exec.argv missing or empty")
	}

	// Pre-spawn: the compiled prompt instructs the named skill to run from
	// this checkout. A missing skill file means the invocation cannot
	// possibly succeed — fail fast and deterministically instead of letting
	// claude discover it mid-run. An empty SkillPath is rejected explicitly
	// before the Join/Stat: filepath.Join(slot.path, "") == slot.path, which
	// always exists, so without this guard an empty value would silently
	// pass the check instead of failing it.
	if cmd.SkillPath == "" {
		return command.InvokeResult{}, errors.New("skill not found: (empty skill_path)")
	}
	if _, statErr := os.Stat(filepath.Join(slot.path, cmd.SkillPath)); statErr != nil {
		return command.InvokeResult{}, fmt.Errorf("skill not found: %s", cmd.SkillPath)
	}

	// TMPDIR: workspace-local so the artifact file is cleaned up
	// automatically with the workspace tempdir (see workspaceTmpDirName).
	tmpDir := filepath.Join(slot.path, workspaceTmpDirName)
	if err := os.MkdirAll(tmpDir, h.cfg.DirPerm); err != nil {
		return command.InvokeResult{}, fmt.Errorf("create tmpdir: %w", err)
	}

	// Env layering, low-to-high priority:
	//   1. Parent process env (PATH, HOME, …) so claude can find its
	//      binary + write to $HOME/.claude state.
	//   2. API key secrets from the last ConfigUpdate (e.g. ANTHROPIC_API_KEY
	//      from api_keys["anthropic"]). These override anything inherited
	//      from the parent — the control-plane key is authoritative.
	//   3. exec.env from the wire. Reserved for non-secret overrides; in
	//      practice the backend sends an empty map now that ANTHROPIC_API_KEY
	//      is delivered via ConfigUpdate.
	//   4. TRACEPARENT from the current ctx so the spawned subprocess
	//      can link its spans into the supervisor's trace (the
	//      supervisor → workspace hop links the same way; this hop
	//      extends it one more step to the Claude Code grand-child).
	env := os.Environ()
	// Personal single-tenant escape hatch: when YAAOS_AGENT_ANTHROPIC_SUBSCRIPTION=true
	// is set on the container, never hand claude an Anthropic API key. Strip any
	// inherited copy and skip the ConfigUpdate injection below so claude falls back
	// to the $HOME/.claude subscription (OAuth) login. No login probe — if nobody is
	// logged in, claude fails, which is acceptable. openai/rwx keys are unaffected.
	subscriptionAuth := os.Getenv("YAAOS_AGENT_ANTHROPIC_SUBSCRIPTION") == "true"
	if subscriptionAuth {
		env = stripEnv(env, "ANTHROPIC_API_KEY")
	}
	if h.cfg.ApiKeys != nil {
		if apiKeys := h.cfg.ApiKeys(); apiKeys != nil {
			for providerID, envVar := range apiKeyProviderEnvVars {
				if subscriptionAuth && providerID == "anthropic" {
					continue
				}
				if sec, ok := apiKeys[providerID]; ok && !sec.IsZero() {
					env = append(env, envVar+"="+sec.Value())
				}
			}
		}
	}
	for k, v := range inv.Exec.Env {
		env = append(env, k+"="+v)
	}
	env = overrideEnv(env, "TMPDIR", tmpDir)
	if tpEnv := tracing.TraceparentEnv(ctx); tpEnv != "" {
		env = append(env, tpEnv)
	}

	// Wall-clock cap on the subprocess comes from
	// `InvokeClaudeCodeCommand.Limits.WallclockSeconds` on the wire,
	// enforced one level up in `supervisor.Pool.Dispatch` via
	// `context.WithTimeout`. Here we just inherit that ctx — the
	// subprocess is killed via SIGTERM/SIGKILL on ctx cancel.
	//
	// Live streaming: pull the Emitter workspace.Run installed into ctx;
	// forward each stream-json line as a progress AgentEvent so the
	// supervisor (and ultimately the SPA's activity view) sees Claude
	// Code's work as it happens, not just at the end. We also accumulate
	// lines into `accumulated` so the terminal InvokeResult still carries
	// the full stdout — the backend's CodeReview step parses the final
	// JSON response out of stdout, not the progress events.
	// `RunStreaming`'s stdout buffer is bypassed when `OnStdoutLine` is
	// set, so this local accumulation is the only place the bytes are
	// retained for the success path.
	emitter := EmitterFromContext(ctx)
	var accumulated bytes.Buffer
	_, endRun := tracing.StartSpan(ctx, "workspace.runclaude")
	res, runErr := h.cfg.RunFunc(ctx, RunStreamingOptions{
		Argv:  inv.Exec.Argv,
		Stdin: []byte(inv.Exec.Stdin),
		Env:   env,
		Dir:   slot.path,
		OnStdoutLine: func(line []byte) {
			accumulated.Write(line)
			accumulated.WriteByte('\n')
			emitter.Progress(map[string]any{
				"workspace_id": cmd.WorkspaceID,
				"stream_line":  string(line),
			})
		},
	})
	endRun(runErr)
	if res != nil {
		// RunStreaming leaves res.Stdout empty when a callback is set;
		// we replace it with the accumulated bytes so downstream code
		// that reads res.Stdout sees the full output.
		res.Stdout = accumulated.Bytes()
	}

	// Artifact collection + conditional push happen for every
	// InvokeClaudeCode exit, regardless of claude's own exit status — a
	// failing invocation may still have written partial output, and a git
	// push failure must not mask a real artifact. Both ship on the
	// InvokeResult below even when this function ultimately returns an
	// error (workspace.executeCommand reads the artifact fields off the
	// Result on both the success and error paths).
	artifactBody, artifactErr := readArtifact(tmpDir, cmd.CommandID)
	pushErr := maybePushOriginHead(ctx, slot.path)

	result := command.InvokeResult{
		WorkspaceID:   cmd.WorkspaceID,
		Artifact:      artifactBody,
		ArtifactError: artifactErr,
	}

	if runErr != nil {
		// `RunStreaming` returns `*exec.ExitError` on non-zero exit with
		// res still populated; surface exit code + stderr + the tail of
		// stdout so the supervisor's failure event carries actionable
		// info. claude with `--output-format=stream-json` emits its
		// `is_error:true` result event on stdout, so stderr alone is
		// usually empty/uninformative. Context cancel / timeout →
		// ctx.Err which the supervisor's pool already maps to a
		// "timeout:" reason.
		var exitErr *exec.ExitError
		if errors.As(runErr, &exitErr) && res != nil {
			return result, fmt.Errorf(
				"claude exit %d: stderr=%s stdout_tail=%s",
				res.ExitCode,
				excerptHead(res.Stderr, errStderrCap),
				excerptTail(res.Stdout, errStdoutTailCap),
			)
		}
		return result, fmt.Errorf("claude subprocess: %w", runErr)
	}
	if pushErr != nil {
		// Exit-push failure is a stage failure with the git stderr — the
		// artifact still ships via `result` above even though this command
		// reports completed_failure.
		return result, fmt.Errorf("git push: %w", pushErr)
	}

	result.ExecResult = command.ExecResult{
		ExitCode: res.ExitCode,
		Stdout:   string(res.Stdout),
		Stderr:   string(res.Stderr),
		Duration: res.Duration,
	}
	return result, nil
}

func (h *RealHandler) RunCodex(ctx context.Context, cmd *command.InvokeCodexCommand) (command.InvokeResult, error) {
	h.mu.Lock()
	slot, ok := h.slots[cmd.Proto.WorkspaceID]
	h.mu.Unlock()
	if !ok {
		return command.InvokeResult{}, ErrUnknownWorkspace
	}

	var inv invocationExec
	if err := json.Unmarshal(cmd.Proto.Invocation, &inv); err != nil {
		return command.InvokeResult{}, fmt.Errorf("decode invocation: %w", err)
	}
	if len(inv.Exec.Argv) == 0 {
		return command.InvokeResult{}, errors.New("invocation.exec.argv missing or empty")
	}

	// Pre-spawn: skill file must exist. Convention: `.codex/skills/<name>/SKILL.md`.
	if cmd.Proto.SkillPath == "" {
		return command.InvokeResult{}, errors.New("skill not found: (empty skill_path)")
	}
	if _, statErr := os.Stat(filepath.Join(slot.path, cmd.Proto.SkillPath)); statErr != nil {
		return command.InvokeResult{}, fmt.Errorf("skill not found: %s", cmd.Proto.SkillPath)
	}

	// TMPDIR: workspace-local so artifact + schema files are cleaned up
	// automatically with the workspace tempdir.
	tmpDir := filepath.Join(slot.path, workspaceTmpDirName)
	if err := os.MkdirAll(tmpDir, h.cfg.DirPerm); err != nil {
		return command.InvokeResult{}, fmt.Errorf("create tmpdir: %w", err)
	}

	argv := append([]string(nil), inv.Exec.Argv...)

	// Output schema: write the JSON Schema to a temp file and append the
	// --output-schema flag so codex constrains its structured output. Only
	// when the backend supplied a schema — nil means no constraint.
	if cmd.Proto.OutputSchemaJSON != "" {
		schemaPath := filepath.Join(tmpDir, cmd.Proto.CommandID+"-schema.json")
		if err := os.WriteFile(schemaPath, []byte(cmd.Proto.OutputSchemaJSON), 0o600); err != nil {
			return command.InvokeResult{}, fmt.Errorf("write output schema: %w", err)
		}
		argv = append(argv, "--output-schema", schemaPath)
	}

	// Env layering (same priority order as RunClaude).
	env := os.Environ()
	if h.cfg.ApiKeys != nil {
		if apiKeys := h.cfg.ApiKeys(); apiKeys != nil {
			for providerID, envVar := range apiKeyProviderEnvVars {
				if sec, ok := apiKeys[providerID]; ok && !sec.IsZero() {
					env = append(env, envVar+"="+sec.Value())
				}
			}
		}
	}
	for k, v := range inv.Exec.Env {
		env = append(env, k+"="+v)
	}
	env = overrideEnv(env, "TMPDIR", tmpDir)
	if tpEnv := tracing.TraceparentEnv(ctx); tpEnv != "" {
		env = append(env, tpEnv)
	}

	emitter := EmitterFromContext(ctx)
	var accumulated bytes.Buffer
	_, endRun := tracing.StartSpan(ctx, "workspace.runcodex")
	res, runErr := h.cfg.RunFunc(ctx, RunStreamingOptions{
		Argv:  argv,
		Stdin: []byte(inv.Exec.Stdin),
		Env:   env,
		Dir:   slot.path,
		OnStdoutLine: func(line []byte) {
			accumulated.Write(line)
			accumulated.WriteByte('\n')
			emitter.Progress(map[string]any{
				"workspace_id": cmd.Proto.WorkspaceID,
				"stream_line":  string(line),
			})
		},
	})
	endRun(runErr)
	if res != nil {
		res.Stdout = accumulated.Bytes()
	}

	artifactBody, artifactErr := readArtifact(tmpDir, cmd.Proto.CommandID)
	pushErr := maybePushOriginHead(ctx, slot.path)

	result := command.InvokeResult{
		WorkspaceID:   cmd.Proto.WorkspaceID,
		Artifact:      artifactBody,
		ArtifactError: artifactErr,
	}

	if runErr != nil {
		var exitErr *exec.ExitError
		if errors.As(runErr, &exitErr) && res != nil {
			return result, fmt.Errorf(
				"codex exit %d: stderr=%s stdout_tail=%s",
				res.ExitCode,
				excerptHead(res.Stderr, errStderrCap),
				excerptTail(res.Stdout, errStdoutTailCap),
			)
		}
		return result, fmt.Errorf("codex subprocess: %w", runErr)
	}
	if pushErr != nil {
		return result, fmt.Errorf("git push: %w", pushErr)
	}

	result.ExecResult = command.ExecResult{
		ExitCode: res.ExitCode,
		Stdout:   string(res.Stdout),
		Stderr:   string(res.Stderr),
		Duration: res.Duration,
	}
	return result, nil
}

// readArtifact reads `$TMPDIR/<command_id>.md`, enforcing artifactMaxBytes.
// Returns (nil, "") when the skill wrote no artifact file — a legitimate
// outcome for review invocations and non-completed main-skill outcomes.
// Returns (nil, "<message>") when the file exists but exceeds the cap (or
// otherwise can't be read) — distinguishes "wrote none" from "wrote too
// much" for the backend. The file is left in place; it dies with the
// workspace tempdir at Cleanup, not here.
//
// Reads via `io.LimitReader(f, artifactMaxBytes+1)` rather than
// `os.Stat` + `os.ReadFile` — a Stat-then-Read pair is a TOCTOU race (the
// file could grow between the two calls, letting a larger-than-cap read
// through); capping the reader itself makes the enforcement atomic with
// the read.
func readArtifact(tmpDir, commandID string) (*string, string) {
	path := filepath.Join(tmpDir, commandID+".md")
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, ""
		}
		return nil, fmt.Sprintf("artifact open failed: %v", err)
	}
	defer func() { _ = f.Close() }()

	data, err := io.ReadAll(io.LimitReader(f, artifactMaxBytes+1))
	if err != nil {
		return nil, fmt.Sprintf("artifact read failed: %v", err)
	}
	if len(data) > artifactMaxBytes {
		return nil, fmt.Sprintf("artifact exceeds %d bytes", artifactMaxBytes)
	}
	body := string(data)
	return &body, ""
}

// headBranchName returns the short name of the branch HEAD points at.
// Returns an error when HEAD is detached (PR-ticket workspaces; review
// flows never commit) or `dir` isn't a git checkout at all — both cases
// mean "nothing to push" to the caller.
func headBranchName(ctx context.Context, dir string) (string, error) {
	c := exec.CommandContext(ctx, "git", "symbolic-ref", "-q", "--short", "HEAD")
	c.Dir = dir
	out, err := c.Output()
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}

// maybePushOriginHead runs `git push origin HEAD` iff HEAD is a named
// branch. Detached checkouts skip silently (nothing to push, not a
// failure). A branch with no new commits is itself a push no-op (git exits
// 0, "Everything up-to-date"). A genuine failure — most commonly a
// non-fast-forward because something rewrote history upstream — surfaces
// via runGit's captured + redacted stderr.
func maybePushOriginHead(ctx context.Context, dir string) error {
	branch, err := headBranchName(ctx, dir)
	if err != nil || branch == "" {
		return nil
	}
	return runGit(ctx, dir, "push", "origin", "HEAD")
}

func (h *RealHandler) Cleanup(_ context.Context, cmd *protocol.CleanupWorkspaceCommand) (command.CleanupResult, error) {
	h.mu.Lock()
	slot, ok := h.slots[cmd.WorkspaceID]
	if ok {
		delete(h.slots, cmd.WorkspaceID)
	}
	h.mu.Unlock()
	if !ok {
		// Idempotent: cleanup of an unknown workspace is a no-op success.
		return command.CleanupResult{
			WorkspaceID: cmd.WorkspaceID,
			Destroyed:   false,
			Reason:      "unknown_workspace",
		}, nil
	}
	if err := os.RemoveAll(slot.path); err != nil {
		return command.CleanupResult{}, fmt.Errorf("cleanup %q: %w", slot.path, err)
	}
	return command.CleanupResult{
		WorkspaceID: cmd.WorkspaceID,
		Destroyed:   true,
		Path:        slot.path,
	}, nil
}

// PushBranch is push-failure recovery only: a bare re-push of the
// workspace's current HEAD after a RefreshWorkspaceAuth credential
// rotation, so claude is never re-run just to retry a push. Re-points
// `origin` at a URL carrying the slot's *current* auth token before
// pushing — RefreshAuth (above) only updates the in-memory slot, never the
// git remote's stored URL, so a push run straight after a credential
// rotation must not fall back to the (now possibly expired) token embedded
// at clone time. Requires HEAD to be a named branch — provision's checkout
// invariant guarantees this for any workspace this command is dispatched
// against.
func (h *RealHandler) PushBranch(ctx context.Context, cmd *protocol.PushBranchCommand) (command.PushBranchResult, error) {
	h.mu.Lock()
	slot, ok := h.slots[cmd.WorkspaceID]
	h.mu.Unlock()
	if !ok {
		return command.PushBranchResult{}, ErrUnknownWorkspace
	}
	branch, err := headBranchName(ctx, slot.path)
	if err != nil || branch == "" {
		return command.PushBranchResult{}, fmt.Errorf("PushBranch: HEAD is not a named branch in %q", slot.path)
	}
	freshURL, err := pushURLWithCurrentToken(slot)
	if err != nil {
		return command.PushBranchResult{}, fmt.Errorf("auth: %w", err)
	}
	if err := runGit(ctx, slot.path, "remote", "set-url", "origin", freshURL); err != nil {
		return command.PushBranchResult{}, fmt.Errorf("git remote set-url: %w", err)
	}
	if err := runGit(ctx, slot.path, "push", "origin", "HEAD"); err != nil {
		return command.PushBranchResult{}, fmt.Errorf("git push: %w", err)
	}
	return command.PushBranchResult{WorkspaceID: cmd.WorkspaceID, Pushed: true}, nil
}

// pushURLWithCurrentToken rebuilds the push URL from the workspace's
// remembered repo.CloneURL and its *current* in-memory auth token — never
// the token embedded in the origin remote's URL at clone time.
// RefreshWorkspaceAuth (see RefreshAuth above) only updates the in-memory
// token; this is the seam that guarantees PushBranch always uses the
// freshest one, so a push run right after a credential rotation can't
// silently fall back to an expired clone-time token.
func pushURLWithCurrentToken(slot *realSlot) (string, error) {
	return injectAuth(slot.repo.CloneURL, protocol.AuthBlock{Kind: slot.authKind, Token: slot.authTok.Value()})
}

// safeJoin guards against path-escape attacks. The supplied `rel` must
// not start with `/`, must not contain `..` segments, and must resolve
// (after Clean) to a subpath of `base`.
func safeJoin(base, rel string) (string, error) {
	if rel == "" {
		return "", errors.New("empty path")
	}
	if filepath.IsAbs(rel) {
		return "", errors.New("absolute path not allowed")
	}
	cleaned := filepath.Clean(rel)
	if strings.HasPrefix(cleaned, "..") || strings.Contains(cleaned, string(filepath.Separator)+"..") {
		return "", errors.New("parent-directory traversal not allowed")
	}
	full := filepath.Join(base, cleaned)
	// Double-check via Rel — defence in depth against any os-specific
	// quirk in Clean/Join.
	rel2, err := filepath.Rel(base, full)
	if err != nil || strings.HasPrefix(rel2, "..") {
		return "", errors.New("path escapes workspace root")
	}
	return full, nil
}

// gitClone shells out to the system `git` binary (present in the runtime
// image — see `apps/agent/Dockerfile`) and produces a working tree at
// `dest`. The auth token is injected into the clone URL via HTTPS basic
// auth (`https://x-access-token:<token>@…`) for GitHub installation
// tokens — that's the supported pattern for short-lived GitHub App
// installation tokens. Other auth kinds use the same `x-access-token`
// form; specialised handling lands when non-GitHub plugins arrive.
//
// Checkout mode is decided by which of `repo.HeadSHA` / `repo.BranchName`
// is set — a well-formed backend command sets exactly one:
//
//   - `repo.HeadSHA` set (today's behaviour, and the only mode a legacy
//     caller with no branch_name ever exercises): detach HEAD at that
//     SHA — fork-safe, works for a PR whose head lives in a fork the
//     agent has no push access to. If `repo.BranchName` is ALSO set
//     (legacy shape — a `--branch` clone-speed hint alongside a required
//     head_sha), it's passed to the initial `git clone --branch` only;
//     HeadSHA still wins the checkout.
//   - `repo.HeadSHA` empty, `repo.BranchName` set: check out that branch
//     as a local work branch (`git checkout -B`), tracking the remote
//     branch when it already exists (falls back to creating a fresh
//     local branch off the clone's default-branch HEAD otherwise) — the
//     mode a pipeline stage that commits + pushes needs.
//
// Sequence:
//  1. `git clone --depth=<history> [--branch=<name>] <url> .` — the
//     `--branch` flag is only added in detached-pin mode (see above);
//     named-branch mode omits it because the branch may not exist on the
//     remote yet.
//  2. Checkout, per the mode above.
//  3. If `base_sha` is set on the wire: `git fetch --depth=1 origin
//     <base_sha>` so the commit becomes a reachable object locally. The
//     review prompt runs `git diff <base_sha>..HEAD` (two-dot range —
//     compares the two trees; no walk of the commit graph between them
//     is needed), so depth=1 of each endpoint is sufficient.
//
// Output is intentionally captured + discarded; failures surface via
// exit code + a sanitized error message that never includes the auth
// token. Token redaction is critical here — git error messages on auth
// failures echo the URL, which would leak the credential.
func gitClone(ctx context.Context, dest string, repo protocol.RepoRef, auth protocol.AuthBlock, history int) error {
	if repo.CloneURL == "" {
		return errors.New("missing clone_url")
	}
	if repo.HeadSHA == "" && repo.BranchName == "" {
		return errors.New("missing head_sha or branch_name")
	}
	cloneURL, err := injectAuth(repo.CloneURL, auth)
	if err != nil {
		return fmt.Errorf("auth: %w", err)
	}
	args := []string{"clone"}
	if history > 0 {
		args = append(args, fmt.Sprintf("--depth=%d", history))
	}
	// The --branch clone hint only applies in detached-pin (head_sha) mode
	// — a same-as-before speed optimization landing the initial clone
	// close to head_sha before the follow-up fetch-by-SHA. In named-branch
	// checkout mode the branch may not exist on the remote yet (a fresh
	// work branch), so passing --branch there would fail the clone
	// outright; checkoutNamedBranch below handles both cases explicitly.
	if repo.HeadSHA != "" && repo.BranchName != "" {
		args = append(args, "--branch", repo.BranchName)
	}
	args = append(args, cloneURL, dest)
	if err := runGit(ctx, "", args...); err != nil {
		return err
	}

	switch {
	case repo.HeadSHA != "":
		if err := checkoutDetachedSHA(ctx, dest, repo.HeadSHA, history); err != nil {
			return err
		}
	case repo.BranchName != "":
		if err := checkoutNamedBranch(ctx, dest, repo.BranchName, history); err != nil {
			return err
		}
	}

	// Fetch base_sha as a reachable object so the review prompt's
	// `git diff base_sha..HEAD` (two-dot) finds both trees locally. No
	// checkout — base only needs to be reachable as a revision. Same
	// unshallowed-fetch fallback as the head path above.
	if repo.BaseSHA != "" {
		baseFetchArgs := []string{"fetch", "--depth=1", "origin", repo.BaseSHA}
		if err := runGit(ctx, dest, baseFetchArgs...); err != nil {
			if err2 := runGit(ctx, dest, "fetch", "origin"); err2 != nil {
				return fmt.Errorf("fetch base fallback: %w", err2)
			}
		}
	}
	return nil
}

// checkoutDetachedSHA pins HEAD to `headSHA`, detached — today's behaviour,
// fork-safe (works for a PR head living in a fork the agent has no push
// access to). A `git rev-parse HEAD` would tell us if the initial clone
// already landed there, but the fetch+checkout pair is cheap enough to
// always run + simpler to reason about.
func checkoutDetachedSHA(ctx context.Context, dest, headSHA string, history int) error {
	fetchDepth := history + 1
	if history == 0 {
		fetchDepth = 1
	}
	fetchArgs := []string{"fetch", fmt.Sprintf("--depth=%d", fetchDepth), "origin", headSHA}
	if err := runGit(ctx, dest, fetchArgs...); err != nil {
		// Some hosts reject fetching by SHA — fall back to a full fetch.
		// This is rare for GitHub (which allows it via the
		// `uploadpack.allowReachableSHA1InWant` config that github.com
		// sets by default) but cheap to defend against.
		if err2 := runGit(ctx, dest, "fetch", "origin"); err2 != nil {
			return fmt.Errorf("fetch fallback: %w", err2)
		}
	}
	if err := runGit(ctx, dest, "checkout", "--detach", headSHA); err != nil {
		return fmt.Errorf("checkout %s: %w", headSHA, err)
	}
	return nil
}

// checkoutNamedBranch checks out `branch` as a local work branch via `git
// checkout -B`. When the remote already has the branch, the local branch is
// created from — and tracks — `origin/<branch>` (git's default
// `branch.autoSetupMerge` behaviour wires the upstream automatically for a
// `-B <name> <remote-tracking-ref>` checkout). When the remote doesn't have
// it yet (a fresh work branch for a pipeline stage that will push it for
// the first time), the local branch is created off whatever ref the
// initial clone landed on (the repo's default branch) with no upstream.
func checkoutNamedBranch(ctx context.Context, dest, branch string, history int) error {
	fetchDepth := history
	if fetchDepth <= 0 {
		fetchDepth = 1
	}
	fetchArgs := []string{"fetch", fmt.Sprintf("--depth=%d", fetchDepth), "origin", branch}
	if err := runGit(ctx, dest, fetchArgs...); err == nil {
		if err := runGit(ctx, dest, "checkout", "-B", branch, "origin/"+branch); err != nil {
			return fmt.Errorf("checkout tracking %s: %w", branch, err)
		}
		return nil
	}
	// Remote doesn't have this branch yet — create a fresh local branch off
	// the clone's current HEAD (the repo's default branch).
	if err := runGit(ctx, dest, "checkout", "-B", branch); err != nil {
		return fmt.Errorf("checkout new branch %s: %w", branch, err)
	}
	return nil
}

// configureGitIdentity sets the workspace's local `git config user.name`/
// `user.email` — the commit identity every skill commit on a named work
// branch needs. Best-effort no-op when both are empty (older wire payloads
// / test fixtures that don't set these fields; detached-checkout review
// flows never commit, so an unset identity there is harmless).
func configureGitIdentity(ctx context.Context, dir, name, email string) error {
	if name != "" {
		if err := runGit(ctx, dir, "config", "user.name", name); err != nil {
			return fmt.Errorf("git config user.name: %w", err)
		}
	}
	if email != "" {
		if err := runGit(ctx, dir, "config", "user.email", email); err != nil {
			return fmt.Errorf("git config user.email: %w", err)
		}
	}
	return nil
}

// injectAuth rewrites the clone URL to carry credentials inline.
// `auth.Token == ""` returns the URL unchanged (the caller may be
// cloning a public repo or a local bare path used in tests).
func injectAuth(rawURL string, auth protocol.AuthBlock) (string, error) {
	if auth.Token == "" {
		return rawURL, nil
	}
	u, err := url.Parse(rawURL)
	if err != nil {
		return "", fmt.Errorf("parse url: %w", err)
	}
	// File and ssh URLs don't take HTTPS basic auth — pass them through.
	if u.Scheme != "https" && u.Scheme != "http" {
		return rawURL, nil
	}
	// GitHub installation tokens authenticate as `x-access-token`; OAuth
	// PATs use the same form. Other providers slot in here when added.
	u.User = url.UserPassword("x-access-token", auth.Token)
	return u.String(), nil
}

// runGit executes `git <args...>` in `cwd` (empty cwd = process default).
// stdout/stderr are captured; on non-zero exit, returns an error
// containing the combined output with the auth token redacted.
func runGit(ctx context.Context, cwd string, args ...string) error {
	cmd := exec.CommandContext(ctx, "git", args...)
	if cwd != "" {
		cmd.Dir = cwd
	}
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("git %s failed: %w (output: %s)",
			args[0], err, redactToken(string(out)))
	}
	return nil
}

// redactToken strips `x-access-token:<…>@` patterns from a string so
// git's own error messages don't leak credentials into logs or events.
// Defensive belt-and-braces: callers should already only pass output to
// log lines they control, but git's error formatting echoes the URL.
func redactToken(s string) string {
	return tokenRedactRe.ReplaceAllString(s, "x-access-token:REDACTED@")
}

// sanitizeID strips characters that aren't safe in a filesystem name.
// We expect UUIDs here (alnum + dashes), so we just filter to that set.
func sanitizeID(id string) string {
	out := make([]byte, 0, len(id))
	for i := 0; i < len(id); i++ {
		c := id[i]
		switch {
		case c >= '0' && c <= '9', c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c == '-', c == '_':
			out = append(out, c)
		}
	}
	if len(out) == 0 {
		return "anon"
	}
	if len(out) > 32 {
		out = out[:32]
	}
	return string(out)
}
