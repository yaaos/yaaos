package workspace

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/secret"
)

// noopClone is a CloneFunc that succeeds without touching the network.
// Tests use this for everything except the real-git tests at the bottom
// of the file so the existing tempdir/WriteFiles/cleanup assertions
// don't depend on git being present in the test container.
func noopClone(context.Context, string, protocol.RepoRef, protocol.AuthBlock, int) error {
	return nil
}

// realHandlerWithNoopClone is the test default.
func realHandlerWithNoopClone(t *testing.T) *RealHandler {
	t.Helper()
	return NewRealHandler(RealHandlerConfig{
		Root:      t.TempDir(),
		CloneFunc: noopClone,
	})
}

func newProvision(workspaceID string) *protocol.ProvisionWorkspaceCommand {
	return &protocol.ProvisionWorkspaceCommand{
		CommandHeader: protocol.CommandHeader{
			CommandID:   "c-provision-" + workspaceID,
			WorkspaceID: workspaceID,
			Kind:        protocol.KindProvisionWorkspace,
		},
		Repo: protocol.RepoRef{
			PluginID:   "github",
			ExternalID: "acme/web",
			CloneURL:   "https://github.com/acme/web.git",
			HeadSHA:    "deadbeef",
		},
		Auth: protocol.AuthBlock{Kind: "github_installation", Token: "tok-abc"},
	}
}

func TestRealHandler_ProvisionWorkspace_AllocatesTempDir(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	res, err := h.ProvisionWorkspace(context.Background(), newProvision("ws-1"))
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if res.Path == "" {
		t.Fatal("result missing Path")
	}
	if _, err := os.Stat(res.Path); err != nil {
		t.Errorf("tempdir not created: %v", err)
	}
	if res.WorkspaceID != "ws-1" {
		t.Errorf("workspace_id: want ws-1 got %q", res.WorkspaceID)
	}
	if res.Repo != "acme/web" {
		t.Errorf("repo: want acme/web got %q", res.Repo)
	}
	// Startup-reconciliation manifest is written for the supervisor to
	// find on restart.
	manifest, err := os.ReadFile(filepath.Join(res.Path, ".workspace-id"))
	if err != nil {
		t.Errorf("manifest read: %v", err)
	}
	if string(manifest) != "ws-1" {
		t.Errorf("manifest contents: want ws-1 got %q", string(manifest))
	}
}

func TestRealHandler_ProvisionWorkspace_CloneFailureTearsDownTempDir(t *testing.T) {
	root := t.TempDir()
	failClone := func(context.Context, string, protocol.RepoRef, protocol.AuthBlock, int) error {
		return errors.New("network exploded")
	}
	h := NewRealHandler(RealHandlerConfig{Root: root, CloneFunc: failClone})
	_, err := h.ProvisionWorkspace(context.Background(), newProvision("ws-fail"))
	if err == nil {
		t.Fatal("want error on clone failure")
	}
	if !strings.Contains(err.Error(), "git clone") {
		t.Errorf("err should prefix with 'git clone', got %q", err.Error())
	}
	// No surviving tempdir under root.
	entries, _ := os.ReadDir(root)
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), "yaaos-ws-") {
			t.Errorf("leaked tempdir on clone failure: %s", e.Name())
		}
	}
	// Slot not registered.
	if _, ok := h.slots["ws-fail"]; ok {
		t.Errorf("slot registered despite clone failure")
	}
}

func TestRealHandler_ProvisionWorkspace_IdempotentOnSecondCall(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	res1, err := h.ProvisionWorkspace(context.Background(), newProvision("ws-1"))
	if err != nil {
		t.Fatalf("create #1: %v", err)
	}
	res2, err := h.ProvisionWorkspace(context.Background(), newProvision("ws-1"))
	if err != nil {
		t.Fatalf("create #2: %v", err)
	}
	if res2.Path != res1.Path {
		t.Errorf("second create should reuse path %q, got %q", res1.Path, res2.Path)
	}
	if !res2.Reused {
		t.Errorf("second create should report Reused=true")
	}
}

func TestRealHandler_WriteFiles_WritesEntries(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	res1, _ := h.ProvisionWorkspace(context.Background(), newProvision("ws-1"))
	wsPath := res1.Path

	files := []protocol.WriteFilesEntry{
		{Path: ".mcp.json", Content: `{"servers":[]}`},
		{Path: "src/foo.py", Content: "print('hi')\n"},
	}
	res, err := h.WriteFiles(context.Background(), &protocol.WriteFilesCommand{
		CommandHeader: protocol.CommandHeader{
			CommandID: "c-write", WorkspaceID: "ws-1", Kind: protocol.KindWriteFiles,
		},
		Files: files,
	})
	if err != nil {
		t.Fatalf("write: %v", err)
	}
	if res.FilesCount != 2 {
		t.Errorf("FilesCount: want 2 got %d", res.FilesCount)
	}
	for _, f := range files {
		path := filepath.Join(wsPath, f.Path)
		got, err := os.ReadFile(path)
		if err != nil {
			t.Errorf("read %s: %v", path, err)
			continue
		}
		if string(got) != f.Content {
			t.Errorf("file %s: want %q got %q", f.Path, f.Content, string(got))
		}
	}
}

func TestRealHandler_WriteFiles_UnknownWorkspace_Errors(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	_, err := h.WriteFiles(context.Background(), &protocol.WriteFilesCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "missing"},
		Files:         []protocol.WriteFilesEntry{{Path: "f", Content: "x"}},
	})
	if !errors.Is(err, ErrUnknownWorkspace) {
		t.Errorf("want ErrUnknownWorkspace, got %v", err)
	}
}

func TestRealHandler_WriteFiles_RejectsPathEscape(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck

	cases := []string{
		"../escape.txt",
		"/etc/passwd",
		"a/../../b",
		"",
	}
	for _, p := range cases {
		_, err := h.WriteFiles(context.Background(), &protocol.WriteFilesCommand{
			CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "ws-1"},
			Files:         []protocol.WriteFilesEntry{{Path: p, Content: "x"}},
		})
		if err == nil {
			t.Errorf("path %q should be rejected", p)
		}
	}
}

func TestRealHandler_RefreshAuth_UpdatesToken(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck
	if got := h.slots["ws-1"].authTok.Value(); got != "tok-abc" {
		t.Fatalf("initial token wrong: got %q", got)
	}

	res, err := h.RefreshAuth(context.Background(), &protocol.RefreshWorkspaceAuthCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-refresh", WorkspaceID: "ws-1"},
		NewToken:      "tok-xyz",
	})
	if err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if !res.Refreshed {
		t.Error("Refreshed: want true got false")
	}
	if got := h.slots["ws-1"].authTok.Value(); got != "tok-xyz" {
		t.Errorf("token after refresh: want tok-xyz got %q", got)
	}
}

func TestRealHandler_RefreshAuth_UnknownWorkspace_Errors(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	_, err := h.RefreshAuth(context.Background(), &protocol.RefreshWorkspaceAuthCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "missing"},
		NewToken:      "x",
	})
	if !errors.Is(err, ErrUnknownWorkspace) {
		t.Errorf("want ErrUnknownWorkspace, got %v", err)
	}
}

func TestRealHandler_Cleanup_RemovesTempDirAndSlot(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	res1, _ := h.ProvisionWorkspace(context.Background(), newProvision("ws-1"))
	wsPath := res1.Path

	res, err := h.Cleanup(context.Background(), &protocol.CleanupWorkspaceCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-clean", WorkspaceID: "ws-1"},
	})
	if err != nil {
		t.Fatalf("cleanup: %v", err)
	}
	if !res.Destroyed {
		t.Errorf("want Destroyed=true, got false")
	}
	if _, err := os.Stat(wsPath); !os.IsNotExist(err) {
		t.Errorf("tempdir still present: %v", err)
	}
	if _, ok := h.slots["ws-1"]; ok {
		t.Errorf("slot not dropped")
	}
}

func TestRealHandler_Cleanup_UnknownWorkspace_IdempotentSuccess(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	res, err := h.Cleanup(context.Background(), &protocol.CleanupWorkspaceCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "ghost"},
	})
	if err != nil {
		t.Fatalf("cleanup of unknown should succeed, got %v", err)
	}
	if res.Destroyed {
		t.Errorf("Destroyed: want false got true")
	}
}

func TestRealHandler_RunClaude_HappyPath_EchoesStdin(t *testing.T) {
	if _, err := exec.LookPath("cat"); err != nil {
		t.Skip("cat not on PATH; subprocess test needs a POSIX shell env")
	}
	h := realHandlerWithNoopClone(t)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck

	// Build an invocation whose exec block runs `cat` — echoes stdin to
	// stdout. Lets us verify the whole pipe: argv resolved, stdin piped,
	// stdout captured, exit_code surfaced.
	execBody := map[string]any{
		"exec": map[string]any{
			"argv":  []string{"cat"},
			"stdin": "hello from the test",
			"env":   map[string]string{},
		},
	}
	rawInv, _ := json.Marshal(execBody)
	res, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInv,
	})
	if err != nil {
		t.Fatalf("invoke: %v", err)
	}
	// Streaming via OnStdoutLine; the per-line accumulator appends a
	// newline after each scanner.Scan() line. cat's stdin had no
	// trailing newline so the accumulated stdout gains one.
	// Strip-and-compare keeps the test robust to that.
	if strings.TrimRight(res.Stdout, "\n") != "hello from the test" {
		t.Errorf("stdout: want 'hello from the test' got %q", res.Stdout)
	}
	if res.ExitCode != 0 {
		t.Errorf("exit_code: want 0 got %d", res.ExitCode)
	}
	if res.Duration == 0 {
		t.Errorf("duration: want non-zero got zero")
	}
}

func TestRealHandler_RunClaude_EnvOverridesParent(t *testing.T) {
	if _, err := exec.LookPath("sh"); err != nil {
		t.Skip("sh not on PATH; subprocess test needs a POSIX shell")
	}
	h := realHandlerWithNoopClone(t)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck

	rawInv, _ := json.Marshal(map[string]any{
		"exec": map[string]any{
			"argv":  []string{"sh", "-c", "echo $FOO_FROM_EXEC"},
			"stdin": "",
			"env":   map[string]string{"FOO_FROM_EXEC": "exec-wins"},
		},
	})
	res, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInv,
	})
	if err != nil {
		t.Fatalf("invoke: %v", err)
	}
	if strings.TrimSpace(res.Stdout) != "exec-wins" {
		t.Errorf("env not propagated: stdout=%q", res.Stdout)
	}
}

func TestRealHandler_RunClaude_CwdIsWorkspacePath(t *testing.T) {
	if _, err := exec.LookPath("pwd"); err != nil {
		t.Skip("pwd not on PATH")
	}
	h := realHandlerWithNoopClone(t)
	cr, _ := h.ProvisionWorkspace(context.Background(), newProvision("ws-1"))
	wsPath := cr.Path

	rawInv, _ := json.Marshal(map[string]any{
		"exec": map[string]any{
			"argv":  []string{"pwd"},
			"stdin": "",
			"env":   map[string]string{},
		},
	})
	res, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInv,
	})
	if err != nil {
		t.Fatalf("invoke: %v", err)
	}
	got := strings.TrimSpace(res.Stdout)
	// macOS adds /private prefix on tempdirs.
	if got != wsPath && got != "/private"+wsPath {
		t.Errorf("cwd: want %q got %q", wsPath, got)
	}
}

func TestRealHandler_RunClaude_NonZeroExit_ReturnsError(t *testing.T) {
	if _, err := exec.LookPath("sh"); err != nil {
		t.Skip("sh not on PATH")
	}
	h := realHandlerWithNoopClone(t)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck

	// claude's --output-format=stream-json puts the diagnostic on stdout,
	// not stderr — assert both halves ride in the error string so the
	// supervisor's FailureReason carries actionable info.
	rawInv, _ := json.Marshal(map[string]any{
		"exec": map[string]any{
			"argv":  []string{"sh", "-c", "echo on-stdout-line; echo error-text >&2; exit 9"},
			"stdin": "",
			"env":   map[string]string{},
		},
	})
	_, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInv,
	})
	if err == nil {
		t.Fatal("want error on non-zero exit")
	}
	for _, want := range []string{"exit 9", "error-text", "on-stdout-line", "stderr=", "stdout_tail="} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("err: want %q substring, got %q", want, err.Error())
		}
	}
}

func TestRealHandler_RunClaude_NonZeroExit_StdoutTailTruncated(t *testing.T) {
	if _, err := exec.LookPath("sh"); err != nil {
		t.Skip("sh not on PATH")
	}
	h := realHandlerWithNoopClone(t)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck

	// Produce >8 KiB of stdout ending in a known sentinel so we can assert
	// the tail (with truncation marker) survives while the head is cut.
	rawInv, _ := json.Marshal(map[string]any{
		"exec": map[string]any{
			"argv": []string{
				"sh", "-c",
				"head -c 8192 /dev/zero | tr '\\0' 'A'; echo; echo END-MARKER; exit 1",
			},
			"stdin": "",
			"env":   map[string]string{},
		},
	})
	_, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-trunc", WorkspaceID: "ws-1"},
		Invocation:    rawInv,
	})
	if err == nil {
		t.Fatal("want error on non-zero exit")
	}
	if !strings.Contains(err.Error(), "[truncated head]") {
		t.Errorf("err: want '[truncated head]' marker, got %q", err.Error())
	}
	if !strings.Contains(err.Error(), "END-MARKER") {
		t.Errorf("err: want tail sentinel 'END-MARKER', got %q", err.Error())
	}
}

func TestRealHandler_RunClaude_MissingArgv_Errors(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck
	rawInv, _ := json.Marshal(map[string]any{"exec": map[string]any{"argv": []string{}}})
	_, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "ws-1"},
		Invocation:    rawInv,
	})
	if err == nil {
		t.Fatal("want error on empty argv")
	}
	if !strings.Contains(err.Error(), "argv missing") {
		t.Errorf("err: want 'argv missing', got %q", err.Error())
	}
}

func TestRealHandler_RunClaude_MalformedInvocation_Errors(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck
	_, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "ws-1"},
		Invocation:    []byte("{not json"),
	})
	if err == nil {
		t.Fatal("want decode error")
	}
	if !strings.Contains(err.Error(), "decode invocation") {
		t.Errorf("err: want 'decode invocation' prefix, got %q", err.Error())
	}
}

func TestRealHandler_RunClaude_UnknownWorkspace_Errors(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	_, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "missing"},
	})
	if !errors.Is(err, ErrUnknownWorkspace) {
		t.Errorf("want ErrUnknownWorkspace, got %v", err)
	}
}

func TestRealHandler_FullLifecycle_ProvisionWriteCleanup(t *testing.T) {
	// End-to-end: drive a fresh workspace through Provision → WriteFiles →
	// Cleanup and assert the file lands then disappears.
	h := realHandlerWithNoopClone(t)
	cr, _ := h.ProvisionWorkspace(context.Background(), newProvision("ws-1"))
	wsPath := cr.Path
	h.WriteFiles(context.Background(), &protocol.WriteFilesCommand{ //nolint:errcheck
		CommandHeader: protocol.CommandHeader{CommandID: "c-w", WorkspaceID: "ws-1"},
		Files:         []protocol.WriteFilesEntry{{Path: "hello.txt", Content: "world"}},
	})
	if _, err := os.Stat(filepath.Join(wsPath, "hello.txt")); err != nil {
		t.Fatalf("file before cleanup: %v", err)
	}
	h.Cleanup(context.Background(), &protocol.CleanupWorkspaceCommand{ //nolint:errcheck
		CommandHeader: protocol.CommandHeader{CommandID: "c-clean", WorkspaceID: "ws-1"},
	})
	if _, err := os.Stat(wsPath); !os.IsNotExist(err) {
		t.Errorf("workspace tree should be gone after cleanup, got %v", err)
	}
}

func TestSafeJoin(t *testing.T) {
	base := "/ws"
	cases := []struct {
		rel  string
		ok   bool
		want string
	}{
		{"a.txt", true, "/ws/a.txt"},
		{"src/foo.py", true, "/ws/src/foo.py"},
		{"./x", true, "/ws/x"},
		{"../escape", false, ""},
		{"/etc/passwd", false, ""},
		{"a/../../b", false, ""},
		{"", false, ""},
	}
	for _, c := range cases {
		got, err := safeJoin(base, c.rel)
		if c.ok && err != nil {
			t.Errorf("safeJoin(%q): want ok, got err=%v", c.rel, err)
		}
		if !c.ok && err == nil {
			t.Errorf("safeJoin(%q): want err, got %q", c.rel, got)
		}
		if c.ok && got != c.want {
			t.Errorf("safeJoin(%q): want %q got %q", c.rel, c.want, got)
		}
	}
}

func TestSanitizeID(t *testing.T) {
	cases := []struct{ in, want string }{
		{"abc-123", "abc-123"},
		{"abc/../etc", "abcetc"},
		{"", "anon"},
		{strings.Repeat("a", 100), strings.Repeat("a", 32)},
	}
	for _, c := range cases {
		if got := sanitizeID(c.in); got != c.want {
			t.Errorf("sanitizeID(%q): want %q got %q", c.in, c.want, got)
		}
	}
}

// ── injectAuth + redactToken unit tests ─────────────────────────────────

func TestInjectAuth_HTTPSGetsToken(t *testing.T) {
	out, err := injectAuth("https://github.com/acme/web.git",
		protocol.AuthBlock{Kind: "github_installation", Token: "ghs_abc"})
	if err != nil {
		t.Fatalf("injectAuth: %v", err)
	}
	if !strings.HasPrefix(out, "https://x-access-token:ghs_abc@") {
		t.Errorf("want x-access-token prefix, got %q", out)
	}
}

func TestInjectAuth_EmptyTokenPreserved(t *testing.T) {
	out, _ := injectAuth("https://github.com/acme/web.git", protocol.AuthBlock{})
	if out != "https://github.com/acme/web.git" {
		t.Errorf("empty token should pass through, got %q", out)
	}
}

func TestInjectAuth_NonHTTPSPassthrough(t *testing.T) {
	// file:// URLs (used in local-bare-repo tests) MUST NOT get HTTPS
	// basic auth — git rejects it.
	out, _ := injectAuth("file:///tmp/bare", protocol.AuthBlock{Token: "x"})
	if out != "file:///tmp/bare" {
		t.Errorf("file:// should pass through, got %q", out)
	}
}

func TestRedactToken_RemovesCredentials(t *testing.T) {
	in := "fatal: could not read Username for 'https://x-access-token:ghs_super_secret@github.com': blah"
	out := redactToken(in)
	if strings.Contains(out, "ghs_super_secret") {
		t.Errorf("redact failed: %q", out)
	}
	if !strings.Contains(out, "REDACTED") {
		t.Errorf("redact didn't insert REDACTED marker: %q", out)
	}
}

func TestRedactToken_NoMatch_Identity(t *testing.T) {
	in := "ordinary string"
	if redactToken(in) != in {
		t.Error("redactToken altered a string with no credentials")
	}
}

// ── Real-git integration: clone from a local bare repo ──────────────────

// localBareRepo creates a fresh bare git repo with a single commit
// containing one file. Returns the file:// URL pointing at it and the
// HEAD commit SHA.
//
// Skips the calling test when `git` isn't on PATH (the agent's Dockerfile
// installs it for production; locally + in the alpine test image we
// apk add it before running CI).
func localBareRepo(t *testing.T) (cloneURL, headSHA string) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git binary not on PATH; install with `apk add git` to exercise real-clone tests")
	}
	tmp := t.TempDir()
	workDir := filepath.Join(tmp, "work")
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		t.Fatalf("mkdir work: %v", err)
	}
	bareDir := filepath.Join(tmp, "bare.git")

	mustRun := func(cwd string, args ...string) string {
		cmd := exec.Command("git", args...)
		cmd.Dir = cwd
		cmd.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=yaaos",
			"GIT_AUTHOR_EMAIL=yaaos@test",
			"GIT_COMMITTER_NAME=yaaos",
			"GIT_COMMITTER_EMAIL=yaaos@test",
		)
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v in %s: %v\n%s", args, cwd, err, out)
		}
		return strings.TrimSpace(string(out))
	}

	mustRun(workDir, "init", "--initial-branch=main")
	if err := os.WriteFile(filepath.Join(workDir, "README.md"), []byte("hello\n"), 0o644); err != nil {
		t.Fatalf("write readme: %v", err)
	}
	mustRun(workDir, "add", ".")
	mustRun(workDir, "commit", "-m", "init")
	mustRun(workDir, "clone", "--bare", workDir, bareDir)
	sha := mustRun(workDir, "rev-parse", "HEAD")
	return "file://" + bareDir, sha
}

// ── RunFunc seam tests ────────────────────────────────────────────────────
//
// These tests exercise RunClaude orchestration through a fake RunFunc so no
// real Claude binary is needed. They cover env layering, emitter forwarding,
// stdout accumulation, non-zero exit mapping, and error propagation.

// fakeRunFunc builds a RunFunc that returns a canned result, optionally
// after calling onCall with the received options. The result's Stdout is
// returned verbatim; callers set it to the expected accumulated bytes.
func fakeRunFunc(result *RunStreamingResult, err error, onCall func(RunStreamingOptions)) RunFunc {
	return func(_ context.Context, opts RunStreamingOptions) (*RunStreamingResult, error) {
		if onCall != nil {
			onCall(opts)
		}
		// Simulate the streaming path: if result has Stdout and no
		// OnStdoutLine is set, return it as-is. If OnStdoutLine is set
		// (which RunClaude always sets), call the callback for each line
		// in Stdout and leave result.Stdout empty — matching RunStreaming's
		// contract so the accumulator in RunClaude fills it.
		if opts.OnStdoutLine != nil && result != nil && len(result.Stdout) > 0 {
			for _, line := range bytes.Split(bytes.TrimRight(result.Stdout, "\n"), []byte("\n")) {
				opts.OnStdoutLine(line)
			}
			r := *result
			r.Stdout = nil
			return &r, err
		}
		if result != nil {
			r := *result
			return &r, err
		}
		return nil, err
	}
}

// rawInvocation builds a JSON invocation blob from argv/stdin/env.
func rawInvocation(t *testing.T, argv []string, stdin string, env map[string]string) []byte {
	t.Helper()
	body := map[string]any{
		"exec": map[string]any{
			"argv":  argv,
			"stdin": stdin,
			"env":   env,
		},
	}
	b, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("rawInvocation: %v", err)
	}
	return b
}

// realHandlerWithFakeRun builds a RealHandler whose RunClaude calls fn
// instead of the real RunStreaming.
func realHandlerWithFakeRun(t *testing.T, fn RunFunc) *RealHandler {
	t.Helper()
	return NewRealHandler(RealHandlerConfig{
		Root:      t.TempDir(),
		CloneFunc: noopClone,
		RunFunc:   fn,
	})
}

// recordingEmitter captures Progress calls in order.
type recordingEmitter struct {
	calls []map[string]any
}

func (e *recordingEmitter) Progress(outputs map[string]any) bool {
	e.calls = append(e.calls, outputs)
	return true
}

func TestRealHandler_RunClaude_FakeRunFunc_HappyPath(t *testing.T) {
	// Happy path: fake RunFunc returns multi-line stdout with ExitCode 0.
	// Asserts: env layering (BYOK key reaches RunFunc via ByokSecrets getter,
	// not via the invocation env), emitter forwarding (each line becomes a
	// progress event), stdout accumulation (RunClaude replaces RunStreaming's
	// empty Stdout with the accumulated bytes).
	cannedOutput := []byte("line-one\nline-two\n")
	var capturedOpts RunStreamingOptions
	fake := fakeRunFunc(
		&RunStreamingResult{Stdout: cannedOutput, ExitCode: 0, Duration: time.Millisecond},
		nil,
		func(opts RunStreamingOptions) { capturedOpts = opts },
	)
	// Wire the BYOK key via the ByokSecrets getter — the invocation env is
	// intentionally empty (backend no longer ships ANTHROPIC_API_KEY there).
	byokKey := secret.New("sk-test-byok")
	h := NewRealHandler(RealHandlerConfig{
		Root:      t.TempDir(),
		CloneFunc: noopClone,
		RunFunc:   fake,
		ByokSecrets: func() map[string]secret.Secret {
			return map[string]secret.Secret{"anthropic": byokKey}
		},
	})
	cr, _ := h.ProvisionWorkspace(context.Background(), newProvision("ws-1"))
	_ = cr

	emitter := &recordingEmitter{}
	ctx := ContextWithEmitter(context.Background(), emitter)

	res, err := h.RunClaude(ctx, &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation: rawInvocation(t,
			[]string{"claude", "--print"},
			"prompt text",
			map[string]string{}, // intentionally empty — BYOK comes from the getter
		),
	})
	if err != nil {
		t.Fatalf("RunClaude: %v", err)
	}

	// env layering: BYOK key must reach RunFunc via the ByokSecrets getter.
	foundBYOK := false
	for _, kv := range capturedOpts.Env {
		if kv == "ANTHROPIC_API_KEY=sk-test-byok" {
			foundBYOK = true
		}
	}
	if !foundBYOK {
		t.Errorf("BYOK key not found in RunFunc env (expected ANTHROPIC_API_KEY=sk-test-byok): %v", capturedOpts.Env)
	}

	// emitter forwarding: two lines → two progress events.
	if len(emitter.calls) != 2 {
		t.Errorf("emitter calls: want 2 got %d", len(emitter.calls))
	} else {
		if emitter.calls[0]["stream_line"] != "line-one" {
			t.Errorf("progress[0].stream_line: want 'line-one' got %v", emitter.calls[0]["stream_line"])
		}
		if emitter.calls[1]["stream_line"] != "line-two" {
			t.Errorf("progress[1].stream_line: want 'line-two' got %v", emitter.calls[1]["stream_line"])
		}
		if emitter.calls[0]["workspace_id"] != "ws-1" {
			t.Errorf("progress.workspace_id: want 'ws-1' got %v", emitter.calls[0]["workspace_id"])
		}
	}

	// stdout accumulation: RunClaude must fill res.Stdout from the
	// accumulator (the fake's OnStdoutLine path left result.Stdout nil).
	if strings.TrimRight(res.Stdout, "\n") != "line-one\nline-two" {
		t.Errorf("accumulated stdout: want 'line-one\\nline-two' got %q", res.Stdout)
	}
	if res.ExitCode != 0 {
		t.Errorf("exit_code: want 0 got %d", res.ExitCode)
	}
}

func TestRealHandler_RunClaude_FakeRunFunc_NonZeroExit_ReturnsError(t *testing.T) {
	// Non-zero exit from the fake: RunClaude must map it to a command error
	// per the error taxonomy (return nil result + error with exit code).
	fake := fakeRunFunc(
		&RunStreamingResult{ExitCode: 42, Stderr: []byte("something failed"), Duration: time.Millisecond},
		// Zero-value ExitError: RunClaude only type-matches it via errors.As and
		// reads ExitCode/Stderr from RunStreamingResult above — it never touches
		// the (nil) ProcessState. If that branch ever calls a method on the
		// matched ExitError, build a real one here instead.
		&exec.ExitError{},
		nil,
	)
	h := realHandlerWithFakeRun(t, fake)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck

	_, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInvocation(t, []string{"claude"}, "", map[string]string{}),
	})
	if err == nil {
		t.Fatal("want error on non-zero exit")
	}
	if !strings.Contains(err.Error(), "exit 42") {
		t.Errorf("err should mention 'exit 42', got %q", err.Error())
	}
	if !strings.Contains(err.Error(), "something failed") {
		t.Errorf("err should include stderr excerpt, got %q", err.Error())
	}
}

func TestRealHandler_RunClaude_FakeRunFunc_RunFuncReturnsError_PropagatesAsCommandError(t *testing.T) {
	// RunFunc returns a plain error (e.g. startup failure). RunClaude must
	// propagate it wrapped as a command error — no panic, no lost error.
	fake := fakeRunFunc(nil, errors.New("subprocess startup failed"), nil)
	h := realHandlerWithFakeRun(t, fake)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck

	_, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInvocation(t, []string{"claude"}, "", map[string]string{}),
	})
	if err == nil {
		t.Fatal("want error when RunFunc returns error")
	}
	if !strings.Contains(err.Error(), "claude subprocess") {
		t.Errorf("err should contain 'claude subprocess', got %q", err.Error())
	}
	if !strings.Contains(err.Error(), "subprocess startup failed") {
		t.Errorf("err should wrap original message, got %q", err.Error())
	}
}

func TestRealHandler_RunClaude_FakeRunFunc_CwdIsWorkspacePath(t *testing.T) {
	// RunFunc receives the workspace tempdir as Dir.
	var capturedDir string
	fake := fakeRunFunc(
		&RunStreamingResult{ExitCode: 0, Duration: time.Millisecond},
		nil,
		func(opts RunStreamingOptions) { capturedDir = opts.Dir },
	)
	h := realHandlerWithFakeRun(t, fake)
	cr, _ := h.ProvisionWorkspace(context.Background(), newProvision("ws-1"))

	h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{ //nolint:errcheck
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInvocation(t, []string{"claude"}, "", map[string]string{}),
	})
	if capturedDir != cr.Path {
		t.Errorf("RunFunc Dir: want %q got %q", cr.Path, capturedDir)
	}
}

func TestRealHandler_ProvisionWorkspace_RealGitClone_LandsHeadSHA(t *testing.T) {
	cloneURL, headSHA := localBareRepo(t)

	h := NewRealHandler(RealHandlerConfig{Root: t.TempDir()})
	cmd := newProvision("ws-real")
	cmd.Repo.CloneURL = cloneURL
	cmd.Repo.HeadSHA = headSHA
	cmd.Repo.BranchName = "main"
	cmd.History = 1
	// File URLs aren't authenticated — empty token to skip auth injection.
	cmd.Auth = protocol.AuthBlock{}

	res, err := h.ProvisionWorkspace(context.Background(), cmd)
	if err != nil {
		t.Fatalf("real clone: %v", err)
	}
	if _, err := os.Stat(filepath.Join(res.Path, "README.md")); err != nil {
		t.Errorf("cloned tree missing README.md: %v", err)
	}

	// HEAD now points at the expected SHA in detached state.
	cmdHead := exec.Command("git", "rev-parse", "HEAD")
	cmdHead.Dir = res.Path
	gotSHA, err := cmdHead.Output()
	if err != nil {
		t.Fatalf("rev-parse HEAD: %v", err)
	}
	if strings.TrimSpace(string(gotSHA)) != headSHA {
		t.Errorf("HEAD: want %s got %s", headSHA, strings.TrimSpace(string(gotSHA)))
	}
}

// localBareRepoWithBase creates a fresh bare git repo with two commits on
// `main` — a base commit and a head commit that adds a second file.
// Returns the file:// URL, the base SHA, and the head SHA.
func localBareRepoWithBase(t *testing.T) (cloneURL, baseSHA, headSHA string) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git binary not on PATH; install with `apk add git` to exercise real-clone tests")
	}
	tmp := t.TempDir()
	workDir := filepath.Join(tmp, "work")
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		t.Fatalf("mkdir work: %v", err)
	}
	bareDir := filepath.Join(tmp, "bare.git")

	mustRun := func(cwd string, args ...string) string {
		cmd := exec.Command("git", args...)
		cmd.Dir = cwd
		cmd.Env = append(os.Environ(),
			"GIT_AUTHOR_NAME=yaaos",
			"GIT_AUTHOR_EMAIL=yaaos@test",
			"GIT_COMMITTER_NAME=yaaos",
			"GIT_COMMITTER_EMAIL=yaaos@test",
		)
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v in %s: %v\n%s", args, cwd, err, out)
		}
		return strings.TrimSpace(string(out))
	}

	mustRun(workDir, "init", "--initial-branch=main")
	if err := os.WriteFile(filepath.Join(workDir, "README.md"), []byte("hello\n"), 0o644); err != nil {
		t.Fatalf("write readme: %v", err)
	}
	mustRun(workDir, "add", ".")
	mustRun(workDir, "commit", "-m", "base")
	baseSHA = mustRun(workDir, "rev-parse", "HEAD")

	if err := os.WriteFile(filepath.Join(workDir, "feature.txt"), []byte("feature\n"), 0o644); err != nil {
		t.Fatalf("write feature: %v", err)
	}
	mustRun(workDir, "add", ".")
	mustRun(workDir, "commit", "-m", "head")
	headSHA = mustRun(workDir, "rev-parse", "HEAD")

	mustRun(workDir, "clone", "--bare", workDir, bareDir)
	return "file://" + bareDir, baseSHA, headSHA
}

// Regression: the agent must fetch base_sha into the shallow clone so
// the review prompt's `git diff base_sha..HEAD` (two-dot) sees both
// trees. Without this fetch, base_sha is unreachable under --depth=1 and
// the diff fails — findings then drift to lines outside the PR diff and
// GitHub's inline-comment API 422s them.
func TestRealHandler_ProvisionWorkspace_RealGitClone_FetchesBaseSHA(t *testing.T) {
	cloneURL, baseSHA, headSHA := localBareRepoWithBase(t)

	h := NewRealHandler(RealHandlerConfig{Root: t.TempDir()})
	cmd := newProvision("ws-base")
	cmd.Repo.CloneURL = cloneURL
	cmd.Repo.HeadSHA = headSHA
	cmd.Repo.BaseSHA = baseSHA
	cmd.Repo.BranchName = "main"
	cmd.History = 1
	cmd.Auth = protocol.AuthBlock{}

	res, err := h.ProvisionWorkspace(context.Background(), cmd)
	if err != nil {
		t.Fatalf("real clone: %v", err)
	}

	// base_sha must be a reachable object inside the cloned dir.
	cmdCat := exec.Command("git", "cat-file", "-e", baseSHA)
	cmdCat.Dir = res.Path
	if out, err := cmdCat.CombinedOutput(); err != nil {
		t.Fatalf("base_sha not reachable after clone: %v\n%s", err, out)
	}

	// `git diff base..HEAD` (two-dot, mirroring the review prompt) must
	// succeed and reflect the new file added in the head commit.
	cmdDiff := exec.Command("git", "diff", "--name-only", baseSHA+"..HEAD")
	cmdDiff.Dir = res.Path
	out, err := cmdDiff.CombinedOutput()
	if err != nil {
		t.Fatalf("git diff base..HEAD: %v\n%s", err, out)
	}
	if !strings.Contains(string(out), "feature.txt") {
		t.Errorf("diff did not include feature.txt; got: %q", out)
	}
}

// ── checkout-mode matrix ─────────────────────────────────────────────────

func TestRealHandler_ProvisionWorkspace_RealGitClone_HeadSHAOnly_StaysDetached(t *testing.T) {
	// No branch_name hint at all — the plainest form of today's behaviour.
	cloneURL, headSHA := localBareRepo(t)

	h := NewRealHandler(RealHandlerConfig{Root: t.TempDir()})
	cmd := newProvision("ws-detached-only")
	cmd.Repo.CloneURL = cloneURL
	cmd.Repo.HeadSHA = headSHA
	cmd.History = 1
	cmd.Auth = protocol.AuthBlock{}

	res, err := h.ProvisionWorkspace(context.Background(), cmd)
	if err != nil {
		t.Fatalf("provision: %v", err)
	}

	symref := exec.Command("git", "symbolic-ref", "-q", "--short", "HEAD")
	symref.Dir = res.Path
	if err := symref.Run(); err == nil {
		t.Error("want detached HEAD (symbolic-ref should fail), got a named branch")
	}
	gotSHA := runGitForTest(t, res.Path, "rev-parse", "HEAD")
	if gotSHA != headSHA {
		t.Errorf("HEAD: want %s got %s", headSHA, gotSHA)
	}
}

func TestRealHandler_ProvisionWorkspace_RealGitClone_BranchName_TracksExistingRemoteBranch(t *testing.T) {
	cloneURL, _ := localBareRepo(t)

	h := NewRealHandler(RealHandlerConfig{Root: t.TempDir()})
	cmd := newProvision("ws-branch-track")
	cmd.Repo.CloneURL = cloneURL
	cmd.Repo.HeadSHA = ""
	cmd.Repo.BranchName = "main"
	cmd.History = 1
	cmd.Auth = protocol.AuthBlock{}

	res, err := h.ProvisionWorkspace(context.Background(), cmd)
	if err != nil {
		t.Fatalf("provision: %v", err)
	}

	branch := runGitForTest(t, res.Path, "symbolic-ref", "--short", "HEAD")
	if branch != "main" {
		t.Errorf("branch: want main got %q", branch)
	}
	upstream := runGitForTest(t, res.Path, "rev-parse", "--abbrev-ref", "main@{upstream}")
	if upstream != "origin/main" {
		t.Errorf("upstream: want origin/main got %q", upstream)
	}
}

func TestRealHandler_ProvisionWorkspace_RealGitClone_BranchName_FreshWhenRemoteMissing(t *testing.T) {
	cloneURL, _ := localBareRepo(t)

	h := NewRealHandler(RealHandlerConfig{Root: t.TempDir()})
	cmd := newProvision("ws-branch-fresh")
	cmd.Repo.CloneURL = cloneURL
	cmd.Repo.HeadSHA = ""
	cmd.Repo.BranchName = "yaaos/work-1"
	cmd.History = 1
	cmd.Auth = protocol.AuthBlock{}

	res, err := h.ProvisionWorkspace(context.Background(), cmd)
	if err != nil {
		t.Fatalf("provision: %v", err)
	}

	branch := runGitForTest(t, res.Path, "symbolic-ref", "--short", "HEAD")
	if branch != "yaaos/work-1" {
		t.Errorf("branch: want yaaos/work-1 got %q", branch)
	}
	upstreamCheck := exec.Command("git", "rev-parse", "--abbrev-ref", "yaaos/work-1@{upstream}")
	upstreamCheck.Dir = res.Path
	if err := upstreamCheck.Run(); err == nil {
		t.Error("want no upstream for a freshly created branch (remote doesn't have it yet)")
	}
	if _, err := os.Stat(filepath.Join(res.Path, "README.md")); err != nil {
		t.Errorf("checkout missing README.md: %v", err)
	}
}

func TestRealHandler_ProvisionWorkspace_MissingCheckoutInstruction_Errors(t *testing.T) {
	h := NewRealHandler(RealHandlerConfig{Root: t.TempDir()})
	cmd := newProvision("ws-missing")
	cmd.Repo.HeadSHA = ""
	cmd.Repo.BranchName = ""

	_, err := h.ProvisionWorkspace(context.Background(), cmd)
	if err == nil {
		t.Fatal("want error when neither head_sha nor branch_name is set")
	}
	if !strings.Contains(err.Error(), "missing head_sha or branch_name") {
		t.Errorf("err: want 'missing head_sha or branch_name' substring, got %q", err.Error())
	}
}

// ── git identity ──────────────────────────────────────────────────────────

func TestRealHandler_ProvisionWorkspace_RealGitClone_SetsGitIdentityFromPayload(t *testing.T) {
	cloneURL, headSHA := localBareRepo(t)

	h := NewRealHandler(RealHandlerConfig{Root: t.TempDir()})
	cmd := newProvision("ws-identity")
	cmd.Repo.CloneURL = cloneURL
	cmd.Repo.HeadSHA = headSHA
	cmd.History = 1
	cmd.Auth = protocol.AuthBlock{}
	cmd.GitUserName = "yaaos"
	cmd.GitUserEmail = "yaaos[bot]@users.noreply.github.com"

	res, err := h.ProvisionWorkspace(context.Background(), cmd)
	if err != nil {
		t.Fatalf("provision: %v", err)
	}

	if got := runGitForTest(t, res.Path, "config", "user.name"); got != "yaaos" {
		t.Errorf("user.name: want yaaos got %q", got)
	}
	if got := runGitForTest(t, res.Path, "config", "user.email"); got != "yaaos[bot]@users.noreply.github.com" {
		t.Errorf("user.email: want yaaos[bot]@users.noreply.github.com got %q", got)
	}
}

func TestRealHandler_ProvisionWorkspace_NoGitIdentityInPayload_IsNoop(t *testing.T) {
	// Older wire payloads / fixtures that don't set GitUserName/GitUserEmail
	// (both zero-value) must not fail provisioning.
	h := realHandlerWithNoopClone(t)
	if _, err := h.ProvisionWorkspace(context.Background(), newProvision("ws-1")); err != nil {
		t.Fatalf("provision without git identity should succeed, got %v", err)
	}
}

// ── PushBranchCommand ─────────────────────────────────────────────────────

func TestRealHandler_PushBranch_ExecutesBarePushAndReportsSuccess(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git binary not on PATH; install with `apk add git` to exercise real-clone push tests")
	}
	cloneURL, _ := localBareRepo(t)
	h := NewRealHandler(RealHandlerConfig{Root: t.TempDir()})
	cmd := newProvision("ws-push")
	cmd.Repo.CloneURL = cloneURL
	cmd.Repo.HeadSHA = ""
	cmd.Repo.BranchName = "main"
	cmd.History = 1
	cmd.Auth = protocol.AuthBlock{}
	cr, err := h.ProvisionWorkspace(context.Background(), cmd)
	if err != nil {
		t.Fatalf("provision: %v", err)
	}

	// Simulate a skill commit on the work branch.
	if err := os.WriteFile(filepath.Join(cr.Path, "new-file.txt"), []byte("new"), 0o644); err != nil {
		t.Fatalf("write file: %v", err)
	}
	runGitForTest(t, cr.Path, "add", ".")
	runGitForTest(t, cr.Path, "commit", "-m", "work")
	wantSHA := runGitForTest(t, cr.Path, "rev-parse", "HEAD")

	res, err := h.PushBranch(context.Background(), &protocol.PushBranchCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-push", WorkspaceID: "ws-push"},
	})
	if err != nil {
		t.Fatalf("PushBranch: %v", err)
	}
	if !res.Pushed {
		t.Error("want Pushed=true")
	}

	out := runGitForTest(t, "", "ls-remote", cloneURL, "refs/heads/main")
	if !strings.Contains(out, wantSHA) {
		t.Errorf("remote main not updated to %s: ls-remote output=%q", wantSHA, out)
	}
}

func TestRealHandler_PushBranch_UnknownWorkspace_Errors(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	_, err := h.PushBranch(context.Background(), &protocol.PushBranchCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "missing"},
	})
	if !errors.Is(err, ErrUnknownWorkspace) {
		t.Errorf("want ErrUnknownWorkspace, got %v", err)
	}
}

func TestRealHandler_PushBranch_DetachedHead_Errors(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git binary not on PATH")
	}
	cloneURL, headSHA := localBareRepo(t)
	h := NewRealHandler(RealHandlerConfig{Root: t.TempDir()})
	cmd := newProvision("ws-detached-push")
	cmd.Repo.CloneURL = cloneURL
	cmd.Repo.HeadSHA = headSHA
	cmd.Auth = protocol.AuthBlock{}
	if _, err := h.ProvisionWorkspace(context.Background(), cmd); err != nil {
		t.Fatalf("provision: %v", err)
	}

	_, err := h.PushBranch(context.Background(), &protocol.PushBranchCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "ws-detached-push"},
	})
	if err == nil {
		t.Fatal("want error when HEAD is detached")
	}
	if !strings.Contains(err.Error(), "not a named branch") {
		t.Errorf("err: want 'not a named branch' substring, got %q", err.Error())
	}
}

func TestPushURLWithCurrentToken_UsesRefreshedTokenNotStaleOne(t *testing.T) {
	// PushBranch must rebuild the push URL from the *current* in-memory
	// token — RefreshWorkspaceAuth only updates realSlot.authTok, never a
	// stored origin-remote URL, so this is the seam that guarantees a push
	// run right after a credential rotation can't silently fall back to a
	// stale clone-time token.
	slot := &realSlot{
		path:     "/unused",
		repo:     protocol.RepoRef{CloneURL: "https://github.com/acme/web.git"},
		authKind: "github_installation",
		authTok:  secret.New("stale-token"),
	}
	slot.authTok = secret.New("fresh-token") // simulates RefreshAuth having run

	got, err := pushURLWithCurrentToken(slot)
	if err != nil {
		t.Fatalf("pushURLWithCurrentToken: %v", err)
	}
	if !strings.Contains(got, "fresh-token") {
		t.Errorf("want fresh-token in push URL, got %q", got)
	}
	if strings.Contains(got, "stale-token") {
		t.Errorf("stale token leaked into push URL: %q", got)
	}
}

// ── skill_path pre-spawn check ───────────────────────────────────────────

func TestRealHandler_RunClaude_SkillNotFound_ReturnsDeterministicFailure(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck

	_, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInvocation(t, []string{"claude"}, "", map[string]string{}),
		SkillPath:     ".claude/skills/pr_review/SKILL.md",
	})
	if err == nil {
		t.Fatal("want error when skill_path is absent from the checkout")
	}
	want := "skill not found: .claude/skills/pr_review/SKILL.md"
	if err.Error() != want {
		t.Errorf("err: want %q got %q", want, err.Error())
	}
}

func TestRealHandler_RunClaude_SkillFound_Proceeds(t *testing.T) {
	fake := fakeRunFunc(&RunStreamingResult{ExitCode: 0, Duration: time.Millisecond}, nil, nil)
	h := realHandlerWithFakeRun(t, fake)
	cr, _ := h.ProvisionWorkspace(context.Background(), newProvision("ws-1"))

	skillRel := ".claude/skills/pr_review/SKILL.md"
	full := filepath.Join(cr.Path, skillRel)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatalf("mkdir skill dir: %v", err)
	}
	if err := os.WriteFile(full, []byte("# skill"), 0o644); err != nil {
		t.Fatalf("write skill file: %v", err)
	}

	_, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInvocation(t, []string{"claude"}, "", map[string]string{}),
		SkillPath:     skillRel,
	})
	if err != nil {
		t.Fatalf("RunClaude: %v (skill file present — should proceed)", err)
	}
}

// ── artifact collection matrix ───────────────────────────────────────────

// tmpDirFromEnv extracts the TMPDIR value RunClaude set on the fake
// RunFunc's env — the same directory the real subprocess would write
// $TMPDIR/<command_id>.md into.
func tmpDirFromEnv(env []string) string {
	for _, kv := range env {
		if rest, ok := strings.CutPrefix(kv, "TMPDIR="); ok {
			return rest
		}
	}
	return ""
}

func TestRealHandler_RunClaude_Artifact_PresentUnderCap(t *testing.T) {
	const cmdID = "c-inv"
	const body = "# Requirements\ndone"
	fake := fakeRunFunc(
		&RunStreamingResult{ExitCode: 0, Duration: time.Millisecond},
		nil,
		func(opts RunStreamingOptions) {
			// Simulate the skill writing $TMPDIR/<command_id>.md before
			// claude exits.
			tmpDir := tmpDirFromEnv(opts.Env)
			if err := os.WriteFile(filepath.Join(tmpDir, cmdID+".md"), []byte(body), 0o644); err != nil {
				t.Fatalf("write artifact: %v", err)
			}
		},
	)
	h := realHandlerWithFakeRun(t, fake)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck

	res, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: cmdID, WorkspaceID: "ws-1"},
		Invocation:    rawInvocation(t, []string{"claude"}, "", map[string]string{}),
	})
	if err != nil {
		t.Fatalf("RunClaude: %v", err)
	}
	if res.Artifact == nil {
		t.Fatal("want Artifact set, got nil")
	}
	if *res.Artifact != body {
		t.Errorf("Artifact: want %q got %q", body, *res.Artifact)
	}
	if res.ArtifactError != "" {
		t.Errorf("ArtifactError: want empty, got %q", res.ArtifactError)
	}
}

func TestRealHandler_RunClaude_Artifact_MissingFile_NilBodyNoError(t *testing.T) {
	fake := fakeRunFunc(&RunStreamingResult{ExitCode: 0, Duration: time.Millisecond}, nil, nil)
	h := realHandlerWithFakeRun(t, fake)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck

	res, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInvocation(t, []string{"claude"}, "", map[string]string{}),
	})
	if err != nil {
		t.Fatalf("RunClaude: %v", err)
	}
	if res.Artifact != nil {
		t.Errorf("Artifact: want nil (skill wrote none), got %q", *res.Artifact)
	}
	if res.ArtifactError != "" {
		t.Errorf("ArtifactError: want empty — a missing file is not an error, got %q", res.ArtifactError)
	}
}

func TestRealHandler_RunClaude_Artifact_OverCap_NilBodyWithError(t *testing.T) {
	const cmdID = "c-inv"
	fake := fakeRunFunc(
		&RunStreamingResult{ExitCode: 0, Duration: time.Millisecond},
		nil,
		func(opts RunStreamingOptions) {
			tmpDir := tmpDirFromEnv(opts.Env)
			oversized := make([]byte, artifactMaxBytes+1)
			if err := os.WriteFile(filepath.Join(tmpDir, cmdID+".md"), oversized, 0o644); err != nil {
				t.Fatalf("write artifact: %v", err)
			}
		},
	)
	h := realHandlerWithFakeRun(t, fake)
	h.ProvisionWorkspace(context.Background(), newProvision("ws-1")) //nolint:errcheck

	res, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: cmdID, WorkspaceID: "ws-1"},
		Invocation:    rawInvocation(t, []string{"claude"}, "", map[string]string{}),
	})
	if err != nil {
		t.Fatalf("RunClaude: %v", err)
	}
	if res.Artifact != nil {
		t.Error("Artifact: want nil for an over-cap file, got non-nil")
	}
	if res.ArtifactError == "" {
		t.Error("ArtifactError: want set to distinguish over-cap from wrote-none, got empty")
	}
}

// ── push-conditionality ───────────────────────────────────────────────────

// runGitForTest runs `git <args...>` in `dir` and fails the test on error.
// Returns trimmed combined output for callers that need it (e.g. ls-remote).
func runGitForTest(t *testing.T, dir string, args ...string) string {
	t.Helper()
	c := exec.Command("git", args...)
	if dir != "" {
		c.Dir = dir
	}
	c.Env = append(os.Environ(),
		"GIT_AUTHOR_NAME=yaaos", "GIT_AUTHOR_EMAIL=yaaos@test",
		"GIT_COMMITTER_NAME=yaaos", "GIT_COMMITTER_EMAIL=yaaos@test",
	)
	out, err := c.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v in %q: %v\n%s", args, dir, err, out)
	}
	return strings.TrimSpace(string(out))
}

func TestRealHandler_RunClaude_PushConditionality(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git binary not on PATH; install with `apk add git` to exercise real-clone push tests")
	}
	successRun := fakeRunFunc(&RunStreamingResult{ExitCode: 0, Duration: time.Millisecond}, nil, nil)

	t.Run("detached_head_skips_push", func(t *testing.T) {
		// Real ProvisionWorkspace always lands detached today (see
		// gitClone) — review-flow workspaces never commit, so RunClaude
		// must not fail trying to push one.
		cloneURL, headSHA := localBareRepo(t)
		h := NewRealHandler(RealHandlerConfig{Root: t.TempDir(), RunFunc: successRun})
		cmd := newProvision("ws-detached")
		cmd.Repo.CloneURL = cloneURL
		cmd.Repo.HeadSHA = headSHA
		cmd.Repo.BranchName = "main"
		cmd.History = 1
		cmd.Auth = protocol.AuthBlock{}
		if _, err := h.ProvisionWorkspace(context.Background(), cmd); err != nil {
			t.Fatalf("provision: %v", err)
		}

		_, err := h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-detached"},
			Invocation:    rawInvocation(t, []string{"claude"}, "", map[string]string{}),
		})
		if err != nil {
			t.Fatalf("RunClaude: %v (detached HEAD should skip push, not fail)", err)
		}
	})

	t.Run("named_branch_with_new_commit_pushes", func(t *testing.T) {
		cloneURL, headSHA := localBareRepo(t)
		h := NewRealHandler(RealHandlerConfig{Root: t.TempDir(), RunFunc: successRun})
		cmd := newProvision("ws-named")
		cmd.Repo.CloneURL = cloneURL
		cmd.Repo.HeadSHA = headSHA
		cmd.Repo.BranchName = "main"
		cmd.History = 1
		cmd.Auth = protocol.AuthBlock{}
		cr, err := h.ProvisionWorkspace(context.Background(), cmd)
		if err != nil {
			t.Fatalf("provision: %v", err)
		}

		// Simulate a checkout onto a named work branch + a skill commit —
		// the durable-before-boundary invariant this push rule serves.
		runGitForTest(t, cr.Path, "checkout", "-b", "work-branch")
		if err := os.WriteFile(filepath.Join(cr.Path, "new-file.txt"), []byte("new"), 0o644); err != nil {
			t.Fatalf("write file: %v", err)
		}
		runGitForTest(t, cr.Path, "add", ".")
		runGitForTest(t, cr.Path, "commit", "-m", "work")

		_, err = h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-named"},
			Invocation:    rawInvocation(t, []string{"claude"}, "", map[string]string{}),
		})
		if err != nil {
			t.Fatalf("RunClaude: %v", err)
		}

		out := runGitForTest(t, "", "ls-remote", cloneURL, "refs/heads/work-branch")
		if out == "" {
			t.Error("expected work-branch to have been pushed to origin; ls-remote returned nothing")
		}
	})

	t.Run("named_branch_no_new_commits_is_noop", func(t *testing.T) {
		cloneURL, headSHA := localBareRepo(t)
		h := NewRealHandler(RealHandlerConfig{Root: t.TempDir(), RunFunc: successRun})
		cmd := newProvision("ws-noop")
		cmd.Repo.CloneURL = cloneURL
		cmd.Repo.HeadSHA = headSHA
		cmd.Repo.BranchName = "main"
		cmd.History = 1
		cmd.Auth = protocol.AuthBlock{}
		cr, err := h.ProvisionWorkspace(context.Background(), cmd)
		if err != nil {
			t.Fatalf("provision: %v", err)
		}

		// Local branch shares the remote's name + SHA — no new commits, so
		// the push is "Everything up-to-date", not a failure. `--branch
		// main` on the initial clone already left a local `main` branch
		// (gitClone only detaches HEAD afterward), so switch to it rather
		// than re-creating it.
		runGitForTest(t, cr.Path, "checkout", "main")

		_, err = h.RunClaude(context.Background(), &protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-noop"},
			Invocation:    rawInvocation(t, []string{"claude"}, "", map[string]string{}),
		})
		if err != nil {
			t.Fatalf("RunClaude: %v (no-op push should not fail)", err)
		}
	})
}
