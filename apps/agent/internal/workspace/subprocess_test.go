package workspace

import (
	"context"
	"errors"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"
)

// shAvailable returns true when /bin/sh is on the test host. All
// streaming subprocess tests depend on /bin/sh (and cat/echo/sleep) —
// the alpine + bookworm images we build under all have these, but
// remain defensive so this file doesn't blow up on a stripped CI image.
func shAvailable(t *testing.T) bool {
	t.Helper()
	if _, err := exec.LookPath("sh"); err != nil {
		t.Skip("sh not on PATH; subprocess streaming tests need a POSIX shell")
		return false
	}
	return true
}

func TestRunStreaming_BufferedStdoutWhenCallbackNil(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "printf 'hello\\nworld\\n'"},
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	if res.ExitCode != 0 {
		t.Errorf("exit_code: want 0 got %d", res.ExitCode)
	}
	if string(res.Stdout) != "hello\nworld\n" {
		t.Errorf("stdout: want 'hello\\nworld\\n' got %q", string(res.Stdout))
	}
	if len(res.Stderr) != 0 {
		t.Errorf("stderr: want empty got %q", string(res.Stderr))
	}
}

func TestRunStreaming_StreamsLinesViaCallback(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	var lines []string
	var mu sync.Mutex
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "printf 'one\\ntwo\\nthree\\n'"},
		OnStdoutLine: func(line []byte) {
			mu.Lock()
			defer mu.Unlock()
			lines = append(lines, string(line))
		},
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	want := []string{"one", "two", "three"}
	if len(lines) != len(want) {
		t.Fatalf("lines: want %v got %v", want, lines)
	}
	for i := range want {
		if lines[i] != want[i] {
			t.Errorf("line %d: want %q got %q", i, want[i], lines[i])
		}
	}
	// When OnStdoutLine is set, Stdout is NOT double-buffered.
	if len(res.Stdout) != 0 {
		t.Errorf("Stdout should be empty when streaming, got %q", string(res.Stdout))
	}
}

func TestRunStreaming_StdinPipedToChild(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv:  []string{"cat"},
		Stdin: []byte("pumpkin\n"),
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	if string(res.Stdout) != "pumpkin\n" {
		t.Errorf("stdout: want 'pumpkin\\n' got %q", string(res.Stdout))
	}
}

func TestRunStreaming_StderrCaptured(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "echo to-stderr 1>&2; echo to-stdout"},
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	if !strings.Contains(string(res.Stderr), "to-stderr") {
		t.Errorf("stderr should contain 'to-stderr', got %q", string(res.Stderr))
	}
	if !strings.Contains(string(res.Stdout), "to-stdout") {
		t.Errorf("stdout should contain 'to-stdout', got %q", string(res.Stdout))
	}
}

func TestRunStreaming_NonZeroExitWrappedAsError(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "echo oops >&2; exit 7"},
	})
	if err == nil {
		t.Fatal("want error on non-zero exit")
	}
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		t.Errorf("want *exec.ExitError, got %T", err)
	}
	// Result still populated.
	if res.ExitCode != 7 {
		t.Errorf("exit_code: want 7 got %d", res.ExitCode)
	}
	if !strings.Contains(string(res.Stderr), "oops") {
		t.Errorf("stderr captured on failure: got %q", string(res.Stderr))
	}
}

func TestRunStreaming_ContextCancel_SignalsProcessGroup(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	// Sleep 30s — we'll cancel after 50ms. The TimedOut flag must be set
	// and total run time stays well under the sleep window.
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	start := time.Now()
	res, err := RunStreaming(ctx, RunStreamingOptions{
		Argv: []string{"sh", "-c", "sleep 30"},
	})
	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("want ctx.Err on cancel")
	}
	if !res.TimedOut {
		t.Errorf("TimedOut should be set on ctx cancel")
	}
	if elapsed > 3*time.Second {
		t.Errorf("cancel didn't terminate promptly: %s", elapsed)
	}
}

func TestRunStreaming_NoArgv_Errors(t *testing.T) {
	_, err := RunStreaming(context.Background(), RunStreamingOptions{})
	if err == nil {
		t.Fatal("want error on empty argv")
	}
	if !strings.Contains(err.Error(), "empty argv") {
		t.Errorf("err: want 'empty argv', got %q", err.Error())
	}
}

func TestRunStreaming_BinaryNotFound_Errors(t *testing.T) {
	_, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"this-binary-definitely-does-not-exist-yaaos-xyzzy"},
	})
	if err == nil {
		t.Fatal("want error on missing binary")
	}
}

func TestRunStreaming_LargeStdinPipedSuccessfully(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	// 256KB of stdin → cat → stdout. Tests that the stdin-writing
	// goroutine doesn't deadlock with the stdout-reading goroutine.
	stdin := make([]byte, 256*1024)
	for i := range stdin {
		stdin[i] = 'a' + byte(i%26)
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv:  []string{"cat"},
		Stdin: stdin,
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	if len(res.Stdout) != len(stdin) {
		t.Errorf("stdout len: want %d got %d", len(stdin), len(res.Stdout))
	}
}

func TestRunStreaming_EnvOverridesParent(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "echo $YAAOS_TEST_VAR"},
		Env:  []string{"YAAOS_TEST_VAR=present", "PATH=/usr/bin:/bin"},
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	if strings.TrimSpace(string(res.Stdout)) != "present" {
		t.Errorf("env var not present: stdout=%q", string(res.Stdout))
	}
}

func TestRunStreaming_DirSetsCwd(t *testing.T) {
	if !shAvailable(t) {
		return
	}
	dir := t.TempDir()
	res, err := RunStreaming(context.Background(), RunStreamingOptions{
		Argv: []string{"sh", "-c", "pwd"},
		Dir:  dir,
	})
	if err != nil {
		t.Fatalf("RunStreaming: %v", err)
	}
	// macOS adds a /private prefix on tempdirs; tolerate it.
	got := strings.TrimSpace(string(res.Stdout))
	if got != dir && got != "/private"+dir {
		t.Errorf("pwd: want %q got %q", dir, got)
	}
}
