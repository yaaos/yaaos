package workspace

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/yaaos/agent/internal/protocol"
)

// noopClone is a CloneFunc that succeeds without touching the network.
// Tests use this for everything except the real-git tests at the bottom
// of the file so the existing tempdir/WriteFiles/cleanup assertions
// don't depend on git being present in the test container.
func noopClone(context.Context, string, protocol.RepoRef, protocol.AuthBlock, int) error {
	return nil
}

// realHandlerWithNoopClone is the test default — drop-in for the slice-65
// constructor calls that pre-dated CloneFunc.
func realHandlerWithNoopClone(t *testing.T) *RealHandler {
	t.Helper()
	return NewRealHandler(RealHandlerConfig{
		Root:      t.TempDir(),
		CloneFunc: noopClone,
	})
}

func newCreate(workspaceID string) *protocol.CreateWorkspaceCommand {
	return &protocol.CreateWorkspaceCommand{
		CommandHeader: protocol.CommandHeader{
			CommandID:   "c-create-" + workspaceID,
			WorkspaceID: workspaceID,
			Kind:        protocol.KindCreateWorkspace,
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

func TestRealHandler_CreateWorkspace_AllocatesTempDir(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	out, err := h.CreateWorkspace(context.Background(), newCreate("ws-1"))
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	path, _ := out["path"].(string)
	if path == "" {
		t.Fatal("output missing path")
	}
	if _, err := os.Stat(path); err != nil {
		t.Errorf("tempdir not created: %v", err)
	}
	if out["repo"] != "acme/web" {
		t.Errorf("repo: want acme/web got %v", out["repo"])
	}
	// Startup-reconciliation manifest is written for the supervisor to
	// find on restart.
	manifest, err := os.ReadFile(filepath.Join(path, ".workspace-id"))
	if err != nil {
		t.Errorf("manifest read: %v", err)
	}
	if string(manifest) != "ws-1" {
		t.Errorf("manifest contents: want ws-1 got %q", string(manifest))
	}
}

func TestRealHandler_CreateWorkspace_CloneFailureTearsDownTempDir(t *testing.T) {
	root := t.TempDir()
	failClone := func(context.Context, string, protocol.RepoRef, protocol.AuthBlock, int) error {
		return errors.New("network exploded")
	}
	h := NewRealHandler(RealHandlerConfig{Root: root, CloneFunc: failClone})
	_, err := h.CreateWorkspace(context.Background(), newCreate("ws-fail"))
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

func TestRealHandler_CreateWorkspace_IdempotentOnSecondCall(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	out1, err := h.CreateWorkspace(context.Background(), newCreate("ws-1"))
	if err != nil {
		t.Fatalf("create #1: %v", err)
	}
	out2, err := h.CreateWorkspace(context.Background(), newCreate("ws-1"))
	if err != nil {
		t.Fatalf("create #2: %v", err)
	}
	if out2["path"] != out1["path"] {
		t.Errorf("second create should reuse path %q, got %q", out1["path"], out2["path"])
	}
	if out2["reused"] != true {
		t.Errorf("second create should report reused=true")
	}
}

func TestRealHandler_WriteFiles_WritesEntries(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	out, _ := h.CreateWorkspace(context.Background(), newCreate("ws-1"))
	wsPath := out["path"].(string)

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
	if got := res["files_count"]; got != 2 {
		t.Errorf("files_count: want 2 got %v", got)
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
	h.CreateWorkspace(context.Background(), newCreate("ws-1"))

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

func TestRealHandler_RefreshWorkspaceAuth_UpdatesToken(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	h.CreateWorkspace(context.Background(), newCreate("ws-1"))
	if got := h.slots["ws-1"].authTok.Value(); got != "tok-abc" {
		t.Fatalf("initial token wrong: got %q", got)
	}

	_, err := h.RefreshWorkspaceAuth(context.Background(), &protocol.RefreshWorkspaceAuthCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-refresh", WorkspaceID: "ws-1"},
		NewToken:      "tok-xyz",
	})
	if err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if got := h.slots["ws-1"].authTok.Value(); got != "tok-xyz" {
		t.Errorf("token after refresh: want tok-xyz got %q", got)
	}
}

func TestRealHandler_RefreshWorkspaceAuth_UnknownWorkspace_Errors(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	_, err := h.RefreshWorkspaceAuth(context.Background(), &protocol.RefreshWorkspaceAuthCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "missing"},
		NewToken:      "x",
	})
	if !errors.Is(err, ErrUnknownWorkspace) {
		t.Errorf("want ErrUnknownWorkspace, got %v", err)
	}
}

func TestRealHandler_CleanupWorkspace_RemovesTempDirAndSlot(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	out, _ := h.CreateWorkspace(context.Background(), newCreate("ws-1"))
	wsPath := out["path"].(string)

	res, err := h.CleanupWorkspace(context.Background(), &protocol.CleanupWorkspaceCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-clean", WorkspaceID: "ws-1"},
	})
	if err != nil {
		t.Fatalf("cleanup: %v", err)
	}
	if res["destroyed"] != true {
		t.Errorf("want destroyed=true, got %v", res["destroyed"])
	}
	if _, err := os.Stat(wsPath); !os.IsNotExist(err) {
		t.Errorf("tempdir still present: %v", err)
	}
	if _, ok := h.slots["ws-1"]; ok {
		t.Errorf("slot not dropped")
	}
}

func TestRealHandler_CleanupWorkspace_UnknownWorkspace_IdempotentSuccess(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	res, err := h.CleanupWorkspace(context.Background(), &protocol.CleanupWorkspaceCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "ghost"},
	})
	if err != nil {
		t.Fatalf("cleanup of unknown should succeed, got %v", err)
	}
	if res["destroyed"] != false {
		t.Errorf("destroyed: want false got %v", res["destroyed"])
	}
}

func TestRealHandler_InvokeClaudeCode_HappyPath_EchoesStdin(t *testing.T) {
	if _, err := exec.LookPath("cat"); err != nil {
		t.Skip("cat not on PATH; subprocess test needs a POSIX shell env")
	}
	h := realHandlerWithNoopClone(t)
	h.CreateWorkspace(context.Background(), newCreate("ws-1"))

	// Build an invocation whose exec block runs `cat` — echoes stdin to
	// stdout. Lets us verify the whole pipe: argv resolved, stdin piped,
	// stdout captured, exit_code surfaced.
	exec := map[string]any{
		"exec": map[string]any{
			"argv":  []string{"cat"},
			"stdin": "hello from the test",
			"env":   map[string]string{},
		},
	}
	rawInv, _ := json.Marshal(exec)
	out, err := h.InvokeClaudeCode(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInv,
	})
	if err != nil {
		t.Fatalf("invoke: %v", err)
	}
	// Streaming via OnStdoutLine; the per-line accumulator
	// appends a newline after each scanner.Scan() line. cat's stdin
	// had no trailing newline so the accumulated stdout gains one.
	// Strip-and-compare keeps the test robust to that.
	if strings.TrimRight(out["stdout"].(string), "\n") != "hello from the test" {
		t.Errorf("stdout: want 'hello from the test' got %q", out["stdout"])
	}
	if out["exit_code"] != 0 {
		t.Errorf("exit_code: want 0 got %v", out["exit_code"])
	}
	if _, ok := out["duration_ms"]; !ok {
		t.Errorf("duration_ms missing from outputs")
	}
}

func TestRealHandler_InvokeClaudeCode_EnvOverridesParent(t *testing.T) {
	if _, err := exec.LookPath("sh"); err != nil {
		t.Skip("sh not on PATH; subprocess test needs a POSIX shell")
	}
	h := realHandlerWithNoopClone(t)
	h.CreateWorkspace(context.Background(), newCreate("ws-1"))

	// `printenv FOO_FROM_EXEC` resolves to the env value we passed
	// through exec.env. Proves the layering picks up exec.env on top of
	// os.Environ().
	rawInv, _ := json.Marshal(map[string]any{
		"exec": map[string]any{
			"argv":  []string{"sh", "-c", "echo $FOO_FROM_EXEC"},
			"stdin": "",
			"env":   map[string]string{"FOO_FROM_EXEC": "exec-wins"},
		},
	})
	out, err := h.InvokeClaudeCode(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInv,
	})
	if err != nil {
		t.Fatalf("invoke: %v", err)
	}
	if strings.TrimSpace(out["stdout"].(string)) != "exec-wins" {
		t.Errorf("env not propagated: stdout=%q", out["stdout"])
	}
}

func TestRealHandler_InvokeClaudeCode_CwdIsWorkspacePath(t *testing.T) {
	if _, err := exec.LookPath("pwd"); err != nil {
		t.Skip("pwd not on PATH")
	}
	h := realHandlerWithNoopClone(t)
	cr, _ := h.CreateWorkspace(context.Background(), newCreate("ws-1"))
	wsPath := cr["path"].(string)

	rawInv, _ := json.Marshal(map[string]any{
		"exec": map[string]any{
			"argv":  []string{"pwd"},
			"stdin": "",
			"env":   map[string]string{},
		},
	})
	out, err := h.InvokeClaudeCode(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInv,
	})
	if err != nil {
		t.Fatalf("invoke: %v", err)
	}
	got := strings.TrimSpace(out["stdout"].(string))
	// macOS adds /private prefix on tempdirs.
	if got != wsPath && got != "/private"+wsPath {
		t.Errorf("cwd: want %q got %q", wsPath, got)
	}
}

func TestRealHandler_InvokeClaudeCode_NonZeroExit_ReturnsError(t *testing.T) {
	if _, err := exec.LookPath("sh"); err != nil {
		t.Skip("sh not on PATH")
	}
	h := realHandlerWithNoopClone(t)
	h.CreateWorkspace(context.Background(), newCreate("ws-1"))

	rawInv, _ := json.Marshal(map[string]any{
		"exec": map[string]any{
			"argv":  []string{"sh", "-c", "echo error-text >&2; exit 9"},
			"stdin": "",
			"env":   map[string]string{},
		},
	})
	_, err := h.InvokeClaudeCode(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-inv", WorkspaceID: "ws-1"},
		Invocation:    rawInv,
	})
	if err == nil {
		t.Fatal("want error on non-zero exit")
	}
	if !strings.Contains(err.Error(), "exit 9") {
		t.Errorf("err: want 'exit 9' substring, got %q", err.Error())
	}
	if !strings.Contains(err.Error(), "error-text") {
		t.Errorf("err should include stderr excerpt, got %q", err.Error())
	}
}

func TestRealHandler_InvokeClaudeCode_MissingArgv_Errors(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	h.CreateWorkspace(context.Background(), newCreate("ws-1"))
	rawInv, _ := json.Marshal(map[string]any{"exec": map[string]any{"argv": []string{}}})
	_, err := h.InvokeClaudeCode(context.Background(), &protocol.InvokeClaudeCodeCommand{
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

func TestRealHandler_InvokeClaudeCode_MalformedInvocation_Errors(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	h.CreateWorkspace(context.Background(), newCreate("ws-1"))
	_, err := h.InvokeClaudeCode(context.Background(), &protocol.InvokeClaudeCodeCommand{
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

func TestRealHandler_InvokeClaudeCode_UnknownWorkspace_Errors(t *testing.T) {
	h := realHandlerWithNoopClone(t)
	_, err := h.InvokeClaudeCode(context.Background(), &protocol.InvokeClaudeCodeCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c", WorkspaceID: "missing"},
	})
	if !errors.Is(err, ErrUnknownWorkspace) {
		t.Errorf("want ErrUnknownWorkspace, got %v", err)
	}
}

func TestRealHandler_FullLifecycle_CreateWriteCleanup(t *testing.T) {
	// End-to-end: drive a fresh workspace through Create → WriteFiles →
	// Cleanup and assert the file lands then disappears.
	h := realHandlerWithNoopClone(t)
	cr, _ := h.CreateWorkspace(context.Background(), newCreate("ws-1"))
	wsPath := cr["path"].(string)
	h.WriteFiles(context.Background(), &protocol.WriteFilesCommand{
		CommandHeader: protocol.CommandHeader{CommandID: "c-w", WorkspaceID: "ws-1"},
		Files:         []protocol.WriteFilesEntry{{Path: "hello.txt", Content: "world"}},
	})
	if _, err := os.Stat(filepath.Join(wsPath, "hello.txt")); err != nil {
		t.Fatalf("file before cleanup: %v", err)
	}
	h.CleanupWorkspace(context.Background(), &protocol.CleanupWorkspaceCommand{
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
	// file:// URLs (used in our local-bare-repo tests) MUST NOT get
	// HTTPS basic auth — git rejects it.
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

func TestRealHandler_CreateWorkspace_RealGitClone_LandsHeadSHA(t *testing.T) {
	cloneURL, headSHA := localBareRepo(t)

	h := NewRealHandler(RealHandlerConfig{Root: t.TempDir()})
	cmd := newCreate("ws-real")
	cmd.Repo.CloneURL = cloneURL
	cmd.Repo.HeadSHA = headSHA
	cmd.Repo.BranchName = "main"
	cmd.History = 1
	// File URLs aren't authenticated — empty token to skip auth injection.
	cmd.Auth = protocol.AuthBlock{}

	out, err := h.CreateWorkspace(context.Background(), cmd)
	if err != nil {
		t.Fatalf("real clone: %v", err)
	}
	path := out["path"].(string)
	if _, err := os.Stat(filepath.Join(path, "README.md")); err != nil {
		t.Errorf("cloned tree missing README.md: %v", err)
	}

	// HEAD now points at the expected SHA in detached state.
	cmdHead := exec.Command("git", "rev-parse", "HEAD")
	cmdHead.Dir = path
	gotSHA, err := cmdHead.Output()
	if err != nil {
		t.Fatalf("rev-parse HEAD: %v", err)
	}
	if strings.TrimSpace(string(gotSHA)) != headSHA {
		t.Errorf("HEAD: want %s got %s", headSHA, strings.TrimSpace(string(gotSHA)))
	}
}
