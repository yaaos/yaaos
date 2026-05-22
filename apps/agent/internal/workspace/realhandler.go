// RealHandler — production workspace.Handler that owns the per-workspace
// tempdir lifecycle. Implements four of five AgentCommand kinds:
//
//   - CreateWorkspace      — `os.MkdirTemp` under the configured root,
//                            stash auth + repo metadata in an in-memory
//                            slot keyed by workspace_id. Git clone is
//                            deferred to a follow-on (it'd need either
//                            the `git` binary in the runtime image or a
//                            pure-Go go-git dep). Emits a structured
//                            `clone_pending=true` output so the backend
//                            can observe the deferral.
//   - WriteFiles           — write each (path, content) entry under the
//                            workspace root. Refuses paths that escape
//                            the root via `..` or absolute components.
//   - RefreshWorkspaceAuth — overwrite the stored auth token in-place.
//                            No I/O — used by the supervisor when the
//                            backend rotates a GitHub installation token
//                            mid-flight.
//   - CleanupWorkspace     — `os.RemoveAll` the tempdir + drop the slot.
//                            Idempotent on a missing workspace_id.
//
// InvokeClaudeCode stays a `not yet implemented` error in this slice —
// it lands when the Claude Code subprocess wrapper is wired (per slice
// 65's TODO note + DECISIONS.md).
//
// Concurrency: a single sync.Mutex serializes slot lookups + mutations.
// Each Handler method is short and non-blocking; the workspace process
// itself dispatches commands single-file via `workspace.Run`, so
// contention is bounded by the supervisor's per-workspace pool serializer.

package workspace

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"

	"github.com/yaaos/agent/internal/protocol"
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
	// image; see Dockerfile slice 69). Tests inject a no-op or a local-
	// bare-repo clone so they don't touch the network.
	CloneFunc CloneFunc
}

// CloneFunc clones `repo` into `dest`. `auth` carries the credential
// kind + token; production uses `github_installation` tokens injected
// into the clone URL via HTTPS basic auth. `history` is the shallow-
// clone depth (`--depth <history>`); pass 0 to skip the flag.
type CloneFunc func(ctx context.Context, dest string, repo protocol.RepoRef, auth protocol.AuthBlock, history int) error

// realSlot tracks one workspace's state across the command sequence.
type realSlot struct {
	path     string // absolute filesystem path of the workspace tempdir
	repo     protocol.RepoRef
	authKind string // "github_installation" | "oauth"
	authTok  string // raw token; never logged
}

// RealHandler implements workspace.Handler for production. Construct
// with NewRealHandler.
type RealHandler struct {
	cfg     RealHandlerConfig
	mu      sync.Mutex
	slots   map[string]*realSlot
}

// NewRealHandler returns a fresh handler with the given config. Use
// `workspace.Run(ctx, in, out, NewRealHandler(...), opts)` from the
// `agent workspace` subcommand entry.
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
	return &RealHandler{cfg: cfg, slots: make(map[string]*realSlot)}
}

// ErrUnknownWorkspace is returned by WriteFiles / RefreshWorkspaceAuth /
// InvokeClaudeCode when no CreateWorkspace has run for the given
// workspace_id. The supervisor surfaces this as a completed_failure
// event; the backend's workflow engine treats it as a fatal step error.
var ErrUnknownWorkspace = errors.New("workspace not created")

func (h *RealHandler) CreateWorkspace(ctx context.Context, cmd *protocol.CreateWorkspaceCommand) (map[string]any, error) {
	h.mu.Lock()
	if _, exists := h.slots[cmd.WorkspaceID]; exists {
		// Idempotent: a second CreateWorkspace for the same id is a
		// supervisor-side bug, but we don't want to crash the workspace
		// process. Keep the existing slot, report reused=true.
		slot := h.slots[cmd.WorkspaceID]
		h.mu.Unlock()
		return map[string]any{
			"workspace_id": cmd.WorkspaceID,
			"path":         slot.path,
			"reused":       true,
		}, nil
	}
	h.mu.Unlock()

	root := h.cfg.Root
	if root == "" {
		root = os.TempDir()
	}
	path, err := os.MkdirTemp(root, "yaaos-ws-"+sanitizeID(cmd.WorkspaceID)+"-")
	if err != nil {
		return nil, fmt.Errorf("mkdir tempdir: %w", err)
	}
	if err := os.Chmod(path, h.cfg.DirPerm); err != nil {
		// Best-effort: the tempdir already exists with default perms.
		// Don't fail the command on chmod.
		_ = err
	}

	// Clone outside the mutex so concurrent CreateWorkspace calls for
	// different workspace_ids don't serialize on the slot map.
	if err := h.cfg.CloneFunc(ctx, path, cmd.Repo, cmd.Auth, cmd.History); err != nil {
		// Tear down the empty tempdir on clone failure so we don't leak.
		_ = os.RemoveAll(path)
		return nil, fmt.Errorf("git clone: %w", err)
	}

	h.mu.Lock()
	defer h.mu.Unlock()
	// Re-check: another goroutine may have raced us in the meantime.
	if existing, raced := h.slots[cmd.WorkspaceID]; raced {
		_ = os.RemoveAll(path)
		return map[string]any{
			"workspace_id": cmd.WorkspaceID,
			"path":         existing.path,
			"reused":       true,
		}, nil
	}
	h.slots[cmd.WorkspaceID] = &realSlot{
		path:     path,
		repo:     cmd.Repo,
		authKind: cmd.Auth.Kind,
		authTok:  cmd.Auth.Token,
	}
	return map[string]any{
		"workspace_id": cmd.WorkspaceID,
		"path":         path,
		"repo":         cmd.Repo.ExternalID,
		"head_sha":     cmd.Repo.HeadSHA,
		"branch":       cmd.Repo.BranchName,
	}, nil
}

func (h *RealHandler) WriteFiles(_ context.Context, cmd *protocol.WriteFilesCommand) (map[string]any, error) {
	h.mu.Lock()
	slot, ok := h.slots[cmd.WorkspaceID]
	h.mu.Unlock()
	if !ok {
		return nil, ErrUnknownWorkspace
	}
	written := 0
	for _, entry := range cmd.Files {
		full, err := safeJoin(slot.path, entry.Path)
		if err != nil {
			return nil, fmt.Errorf("file %q: %w", entry.Path, err)
		}
		if err := os.MkdirAll(filepath.Dir(full), h.cfg.DirPerm); err != nil {
			return nil, fmt.Errorf("file %q: mkdir parent: %w", entry.Path, err)
		}
		if err := os.WriteFile(full, []byte(entry.Content), h.cfg.FilePerm); err != nil {
			return nil, fmt.Errorf("file %q: write: %w", entry.Path, err)
		}
		written++
	}
	return map[string]any{
		"workspace_id": cmd.WorkspaceID,
		"files_count":  written,
	}, nil
}

func (h *RealHandler) RefreshWorkspaceAuth(_ context.Context, cmd *protocol.RefreshWorkspaceAuthCommand) (map[string]any, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	slot, ok := h.slots[cmd.WorkspaceID]
	if !ok {
		return nil, ErrUnknownWorkspace
	}
	slot.authTok = cmd.NewToken
	return map[string]any{
		"workspace_id": cmd.WorkspaceID,
		"refreshed":    true,
	}, nil
}

func (h *RealHandler) InvokeClaudeCode(_ context.Context, cmd *protocol.InvokeClaudeCodeCommand) (map[string]any, error) {
	h.mu.Lock()
	_, ok := h.slots[cmd.WorkspaceID]
	h.mu.Unlock()
	if !ok {
		return nil, ErrUnknownWorkspace
	}
	// Real Claude Code subprocess wiring lands in a follow-on slice.
	// Returning an explicit error here surfaces the gap to the backend's
	// workflow engine rather than silently completing the step.
	return nil, errors.New("InvokeClaudeCode: subprocess wiring not yet implemented (Phase 6 follow-on)")
}

func (h *RealHandler) CleanupWorkspace(_ context.Context, cmd *protocol.CleanupWorkspaceCommand) (map[string]any, error) {
	h.mu.Lock()
	slot, ok := h.slots[cmd.WorkspaceID]
	if ok {
		delete(h.slots, cmd.WorkspaceID)
	}
	h.mu.Unlock()
	if !ok {
		// Idempotent: cleanup of an unknown workspace is a no-op success.
		return map[string]any{
			"workspace_id": cmd.WorkspaceID,
			"destroyed":    false,
			"reason":       "unknown_workspace",
		}, nil
	}
	if err := os.RemoveAll(slot.path); err != nil {
		return nil, fmt.Errorf("cleanup %q: %w", slot.path, err)
	}
	return map[string]any{
		"workspace_id": cmd.WorkspaceID,
		"destroyed":    true,
		"path":         slot.path,
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
// same `x-access-token` form for now; specialised handling lands when
// non-GitHub plugins arrive.
//
// Sequence:
//   1. `git clone --depth=<history> [--branch=<name>] <url> .`
//   2. If `head_sha` differs from what HEAD resolves to:
//      `git fetch --depth=<history+1> origin <head_sha>` then
//      `git checkout <head_sha>`.
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
