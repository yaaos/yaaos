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
//   - RunClaude            — read `invocation.exec` from the wire
//                            ({argv, stdin, env}), merge env
//                            on top of `os.Environ()`, add TRACEPARENT
//                            for span linkage, dispatch via the
//                            configured `RunFunc` (production default:
//                            `RunStreaming`) with the workspace tempdir
//                            as cwd. Captured stdout is returned in the
//                            typed InvokeResult. Zero biz logic — the
//                            backend owns prompt assembly + argv flags.
//   - Cleanup              — `os.RemoveAll` the tempdir + drop the slot.
//                            Idempotent on a missing workspace_id.
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
// event; the backend's workflow engine treats it as a fatal step error.
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

	// Env layering, low-to-high priority:
	//   1. Parent process env (PATH, HOME, …) so claude can find its
	//      binary + write to $HOME/.claude state.
	//   2. exec.env from the wire (ANTHROPIC_API_KEY, etc.). Backend-
	//      supplied secrets win over anything the parent inherited.
	//   3. TRACEPARENT from the current ctx so the spawned subprocess
	//      can link its spans into the supervisor's trace (the
	//      supervisor → workspace hop links the same way; this hop
	//      extends it one more step to the Claude Code grand-child).
	env := os.Environ()
	for k, v := range inv.Exec.Env {
		env = append(env, k+"="+v)
	}
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
	res, err := h.cfg.RunFunc(ctx, RunStreamingOptions{
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
	endRun(err)
	if res != nil {
		// RunStreaming leaves res.Stdout empty when a callback is set;
		// we replace it with the accumulated bytes so downstream code
		// that reads res.Stdout sees the full output.
		res.Stdout = accumulated.Bytes()
	}
	if err != nil {
		// `RunStreaming` returns `*exec.ExitError` on non-zero exit with
		// res still populated; we surface stderr + exit code via the
		// returned error so the supervisor's failure event carries
		// actionable info. Context cancel / timeout → ctx.Err which the
		// supervisor's pool already maps to a "timeout:" reason.
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) && res != nil {
			stderrExcerpt := string(res.Stderr)
			if len(stderrExcerpt) > 2048 {
				stderrExcerpt = stderrExcerpt[:2048] + "...[truncated]"
			}
			return command.InvokeResult{}, fmt.Errorf("claude exit %d: %s", res.ExitCode, stderrExcerpt)
		}
		return command.InvokeResult{}, fmt.Errorf("claude subprocess: %w", err)
	}

	return command.InvokeResult{
		WorkspaceID: cmd.WorkspaceID,
		ExecResult: command.ExecResult{
			ExitCode: res.ExitCode,
			Stdout:   string(res.Stdout),
			Stderr:   string(res.Stderr),
			Duration: res.Duration,
		},
	}, nil
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
// `dest` checked out to `repo.HeadSHA`. The auth token is injected into
// the clone URL via HTTPS basic auth (`https://x-access-token:<token>@…`)
// for GitHub installation tokens — that's the supported pattern for
// short-lived GitHub App installation tokens. Other auth kinds use the
// same `x-access-token` form; specialised handling lands when
// non-GitHub plugins arrive.
//
// Sequence:
//  1. `git clone --depth=<history> [--branch=<name>] <url> .`
//  2. If `head_sha` differs from what HEAD resolves to:
//     `git fetch --depth=<history+1> origin <head_sha>` then
//     `git checkout <head_sha>`.
//  3. If `base_sha` is set on the wire:
//     `git fetch --depth=1 origin <base_sha>` so the commit becomes a
//     reachable object locally. The review prompt runs
//     `git diff <base_sha>..HEAD` (two-dot range — compares the two
//     trees; no walk of the commit graph between them is needed), so
//     depth=1 of each endpoint is sufficient.
//
// Step 2 covers two cases the supervisor flow exercises: branch-name
// supplied + HEAD has moved since the webhook fired (the wire's head_sha
// is authoritative), and branch-name omitted (clone defaults to the
// repo's default branch — we then pin to head_sha).
//
// Output is intentionally captured + discarded; failures surface via
// exit code + a sanitized error message that never includes the auth
// token. Token redaction is critical here — git error messages on auth
// failures echo the URL, which would leak the credential.
func gitClone(ctx context.Context, dest string, repo protocol.RepoRef, auth protocol.AuthBlock, history int) error {
	if repo.CloneURL == "" {
		return errors.New("missing clone_url")
	}
	if repo.HeadSHA == "" {
		return errors.New("missing head_sha")
	}
	cloneURL, err := injectAuth(repo.CloneURL, auth)
	if err != nil {
		return fmt.Errorf("auth: %w", err)
	}
	args := []string{"clone"}
	if history > 0 {
		args = append(args, fmt.Sprintf("--depth=%d", history))
	}
	if repo.BranchName != "" {
		args = append(args, "--branch", repo.BranchName)
	}
	args = append(args, cloneURL, dest)
	if err := runGit(ctx, "", args...); err != nil {
		return err
	}
	// Pin to head_sha. A `git rev-parse HEAD` would tell us if we already
	// landed there from the clone, but the fetch+checkout pair is cheap
	// enough to always run + simpler to reason about.
	fetchDepth := history + 1
	if history == 0 {
		fetchDepth = 1
	}
	fetchArgs := []string{"fetch", fmt.Sprintf("--depth=%d", fetchDepth), "origin", repo.HeadSHA}
	if err := runGit(ctx, dest, fetchArgs...); err != nil {
		// Some hosts reject fetching by SHA — fall back to a full fetch.
		// This is rare for GitHub (which allows it via the
		// `uploadpack.allowReachableSHA1InWant` config that github.com
		// sets by default) but cheap to defend against.
		if err2 := runGit(ctx, dest, "fetch", "origin"); err2 != nil {
			return fmt.Errorf("fetch fallback: %w", err2)
		}
	}
	if err := runGit(ctx, dest, "checkout", "--detach", repo.HeadSHA); err != nil {
		return fmt.Errorf("checkout %s: %w", repo.HeadSHA, err)
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
