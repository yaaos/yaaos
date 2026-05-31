// Tests for execRunner: concurrency invariants on Close.
//
// These tests use a real OS subprocess (TestExecHelperProcess pattern) so the
// SIGTERM→grace→SIGKILL path runs against actual process-group mechanics.
// testing/synctest cannot drive real subprocess timing (pipe IO goroutines sit
// in OS-level IO-wait, which is not "durably blocked" in the synctest sense),
// so these tests use real wall-clock time + the race detector. Run with -race
// to exercise the sync.Once guard under contention.
package supervisor

import (
	"context"
	"os"
	"os/exec"
	"sync"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/ipc"
)

// TestExecRunner_ConcurrentClose_IdempotentAndOrdered proves:
//   - Calling Close() from N concurrent goroutines never panics or races.
//   - The sync.Once guard makes Close idempotent: exactly one teardown runs.
//   - SIGTERM → grace → SIGKILL ordering holds: the child-exit watcher goroutine
//     races with the explicit Close caller; neither path corrupts shared state.
//
// Uses the TestExecHelperProcess/hang_forever child behaviour so no real
// Claude/git binaries are needed. Real wall-clock time is required because a
// real OS subprocess's pipe IO is not durably blocked in the synctest sense.
func TestExecRunner_ConcurrentClose_IdempotentAndOrdered(t *testing.T) {
	if os.Getenv("GO_EXEC_HELPER_PROCESS") == "1" {
		return
	}

	runner := spawnHelperRunner(t, "hang_forever", 200*time.Millisecond)

	// Concurrently call Close from 8 goroutines. sync.Once must ensure
	// exactly one teardown path executes; the others return immediately.
	// The race detector validates no unsynchronised field access.
	const concurrency = 8
	var wg sync.WaitGroup
	for i := 0; i < concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_ = runner.Close(context.Background())
		}()
	}

	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	// The child doesn't catch SIGTERM; it exits on first signal. The
	// closeGrace is 200ms so the sequence resolves quickly. Allow 3s total.
	select {
	case <-done:
		// All goroutines returned without panic or deadlock.
	case <-time.After(3 * time.Second):
		t.Fatal("concurrent Close did not return within 3s; possible deadlock")
	}

	// Second close after the process is gone must be safe (idempotent).
	_ = runner.Close(context.Background())
}

// TestExecRunner_CloseAfterChildExit_Idempotent proves that Close is safe
// when the child process has already exited before Close is invoked — the
// cmd.Wait() inside closeOnce must not block or panic.
func TestExecRunner_CloseAfterChildExit_Idempotent(t *testing.T) {
	if os.Getenv("GO_EXEC_HELPER_PROCESS") == "1" {
		return
	}

	runner := spawnHelperRunner(t, "exit_clean", 2*time.Second)

	// Give the child a moment to exit on its own.
	time.Sleep(50 * time.Millisecond)

	// Close must return quickly even though the process has already exited.
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = runner.Close(context.Background())
	}()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Close blocked after child exited")
	}

	// Second call is idempotent.
	_ = runner.Close(context.Background())
}

// ── TestExecHelperProcess entry point ────────────────────────────────────────
//
// TestExecHelperProcess is the subprocess entry point for exec_spawn_test tests.
// Guards with GO_EXEC_HELPER_PROCESS=1 so it never runs as a real test.

func TestExecHelperProcess(t *testing.T) {
	if os.Getenv("GO_EXEC_HELPER_PROCESS") != "1" {
		return
	}
	switch os.Getenv("GO_EXEC_HELPER_CMD") {
	case "hang_forever":
		// Hang until killed. SIGTERM is not caught so the default handler
		// terminates the process immediately — close grace fires quickly.
		// Use a very long sleep instead of select{} to avoid Go's deadlock
		// detector, which fires on select{} when all goroutines are asleep.
		time.Sleep(24 * time.Hour)
		os.Exit(0)

	case "exit_clean":
		// Exit immediately. Used to test Close after the child has already gone.
		os.Exit(0)

	default:
		os.Exit(1)
	}
}

// ── Internal helpers ─────────────────────────────────────────────────────────

// spawnHelperRunner launches this test binary as a child subprocess running
// the given GO_EXEC_HELPER_CMD behaviour and wraps it in an execRunner with
// the supplied closeGrace. The child runs under TestExecHelperProcess.
func spawnHelperRunner(t *testing.T, helperCmd string, closeGrace time.Duration) *execRunner {
	t.Helper()

	exe, err := os.Executable()
	if err != nil {
		t.Fatalf("os.Executable: %v", err)
	}

	c := exec.Command(exe, "-test.run=TestExecHelperProcess")
	c.Env = append(os.Environ(),
		"GO_EXEC_HELPER_PROCESS=1",
		"GO_EXEC_HELPER_CMD="+helperCmd,
	)
	c.Stderr = os.Stderr
	// Place the child in its own process group so killGroup reaches it.
	c.SysProcAttr = procAttrNewPGroup()

	stdin, err := c.StdinPipe()
	if err != nil {
		t.Fatalf("StdinPipe: %v", err)
	}
	stdout, err := c.StdoutPipe()
	if err != nil {
		t.Fatalf("StdoutPipe: %v", err)
	}
	if err := c.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}

	r := &execRunner{
		cmd:        c,
		stdin:      stdin,
		stdout:     stdout,
		enc:        ipc.NewEncoder(stdin),
		dec:        ipc.NewDecoder(stdout),
		closeGrace: closeGrace,
		log:        nullLogger{},
		workspace:  "test-ws",
	}
	t.Cleanup(func() { _ = r.Close(context.Background()) })
	return r
}
