package logging

import (
	"bytes"
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestResolveLogDir_DefaultsAndOverride(t *testing.T) {
	t.Setenv("YAAOS_LOG_DIR", "")
	if got := ResolveLogDir(); got != defaultLogDir {
		t.Fatalf("default: want %q, got %q", defaultLogDir, got)
	}
	t.Setenv("YAAOS_LOG_DIR", "/tmp/yaaos-test")
	if got := ResolveLogDir(); got != "/tmp/yaaos-test" {
		t.Fatalf("override: want /tmp/yaaos-test, got %q", got)
	}
}

func TestInit_WritesToFile(t *testing.T) {
	dir := t.TempDir()
	var stdout bytes.Buffer
	shutdown, err := Init(Config{LogDir: dir, StdoutWriter: &stdout})
	if err != nil {
		t.Fatalf("Init: %v", err)
	}
	t.Cleanup(func() { _ = shutdown(context.Background()) })

	slog.Info("hello.from.test", "k", "v")

	// lumberjack writes synchronously, no flush needed
	body, err := os.ReadFile(filepath.Join(dir, "agent.log"))
	if err != nil {
		t.Fatalf("read log: %v", err)
	}
	if !strings.Contains(string(body), "hello.from.test") {
		t.Fatalf("agent.log missing message; got:\n%s", body)
	}
	if !strings.Contains(string(body), `k=v`) {
		t.Fatalf("agent.log missing key=value; got:\n%s", body)
	}
}

func TestInit_TeesStdoutAndFile(t *testing.T) {
	dir := t.TempDir()
	var stdout bytes.Buffer
	shutdown, err := Init(Config{LogDir: dir, StdoutWriter: &stdout})
	if err != nil {
		t.Fatalf("Init: %v", err)
	}
	t.Cleanup(func() { _ = shutdown(context.Background()) })

	slog.Info("tee.check")

	if !strings.Contains(stdout.String(), "tee.check") {
		t.Fatalf("stdout missing message; got:\n%s", stdout.String())
	}
	body, _ := os.ReadFile(filepath.Join(dir, "agent.log"))
	if !strings.Contains(string(body), "tee.check") {
		t.Fatalf("file missing message; got:\n%s", body)
	}
}

func TestInit_FallsBackOnUnwritableDir(t *testing.T) {
	// Create a regular file, then ask Init to use a path *inside* it as the
	// log directory. MkdirAll fails because you can't make a directory
	// inside a file. Init must continue with stdout-only and not panic.
	parent := t.TempDir()
	blocker := filepath.Join(parent, "blocker")
	if err := os.WriteFile(blocker, []byte("x"), 0o644); err != nil {
		t.Fatalf("seed blocker: %v", err)
	}
	badDir := filepath.Join(blocker, "logs")

	var stdout bytes.Buffer
	shutdown, err := Init(Config{LogDir: badDir, StdoutWriter: &stdout})
	if err != nil {
		t.Fatalf("Init must not error on unwritable dir, got: %v", err)
	}
	t.Cleanup(func() { _ = shutdown(context.Background()) })

	slog.Info("fallback.check")
	if !strings.Contains(stdout.String(), "fallback.check") {
		t.Fatalf("stdout still receives logs; got:\n%s", stdout.String())
	}
}

func TestInit_ExtraHandlerReceivesRecords(t *testing.T) {
	// The OTel slog bridge plugs in via Config.ExtraHandlers.
	// Verify the fan-out delivers to extras.
	captured := &captureHandler{}
	dir := t.TempDir()
	var stdout bytes.Buffer
	shutdown, err := Init(Config{
		LogDir:        dir,
		StdoutWriter:  &stdout,
		ExtraHandlers: []slog.Handler{captured},
	})
	if err != nil {
		t.Fatalf("Init: %v", err)
	}
	t.Cleanup(func() { _ = shutdown(context.Background()) })

	slog.Info("extra.fanout")

	// Init itself emits one "agent.logging.initialized" record before our
	// test call, so look for the test message anywhere in the captured set.
	var found bool
	for _, r := range captured.records {
		if r.Message == "extra.fanout" {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("extra handler missing extra.fanout; saw %d records", len(captured.records))
	}
}

// captureHandler is a minimal slog.Handler that records every Handle call —
// used only to assert fan-out delivery in tests.
type captureHandler struct {
	records []slog.Record
}

func (c *captureHandler) Enabled(context.Context, slog.Level) bool { return true }
func (c *captureHandler) Handle(_ context.Context, r slog.Record) error {
	c.records = append(c.records, r)
	return nil
}
func (c *captureHandler) WithAttrs([]slog.Attr) slog.Handler { return c }
func (c *captureHandler) WithGroup(string) slog.Handler      { return c }
