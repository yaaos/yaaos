// Package logging owns the workspace agent's slog fan-out. One Init at
// startup wires slog.Default() to a multi-handler that writes to:
//
//   - stdout (always; preserves ECS awslogs → CloudWatch pipeline)
//   - a rotated file under YAAOS_LOG_DIR (default /var/log/yaaos-agent),
//     so the operator can pull logs out-of-band when the backend is
//     unreachable. Lumberjack handles rotation + 3-day age-based prune
//   - any caller-supplied extra handlers (the OTel slog bridge plugs
//     in here in commit C)
//
// Init never fatally errors: if the log directory is unwritable, it
// emits a warning to stderr and continues with stdout-only. Crashing
// the agent because we can't open a log file would be ironic and
// operator-hostile.
package logging

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"

	"gopkg.in/natefinch/lumberjack.v2"
)

const (
	defaultLogDir = "/var/log/yaaos-agent"
	logFileName   = "agent.log"

	// Rotation defaults. 50 MB × 10 backups = ~500 MB worst-case
	// uncompressed (much less with Compress: true). MaxAge: 3 days
	// is the user-requested prune horizon.
	defaultMaxSizeMB  = 50
	defaultMaxBackups = 10
	defaultMaxAgeDays = 3
)

// Config configures Init. Zero-value defaults are documented per-field.
type Config struct {
	// LogDir is the directory where rotated log files are written.
	// Empty → resolved from YAAOS_LOG_DIR env var, falling back to
	// /var/log/yaaos-agent.
	LogDir string

	// StdoutWriter overrides os.Stdout. Tests inject a *bytes.Buffer;
	// production leaves this nil.
	StdoutWriter io.Writer

	// ExtraHandlers are appended to the fan-out after stdout + file.
	// Commit C passes the OTel slog bridge here.
	ExtraHandlers []slog.Handler

	// MaxSizeMB / MaxBackups / MaxAgeDays override lumberjack defaults.
	// Zero → use the package default.
	MaxSizeMB  int
	MaxBackups int
	MaxAgeDays int
}

// ResolveLogDir returns LogDir if non-empty, else $YAAOS_LOG_DIR, else
// the package default. Exported so callers can log the resolved value.
func ResolveLogDir() string {
	if v := os.Getenv("YAAOS_LOG_DIR"); v != "" {
		return v
	}
	return defaultLogDir
}

// Init wires slog.Default() to the fan-out handler. The returned
// shutdown func closes the rotated-file writer; safe to call multiple
// times. Callers should defer shutdown from main.
func Init(cfg Config) (shutdown func(context.Context) error, err error) {
	stdout := cfg.StdoutWriter
	if stdout == nil {
		stdout = os.Stdout
	}
	dir := cfg.LogDir
	if dir == "" {
		dir = ResolveLogDir()
	}
	maxSize := cfg.MaxSizeMB
	if maxSize == 0 {
		maxSize = defaultMaxSizeMB
	}
	maxBackups := cfg.MaxBackups
	if maxBackups == 0 {
		maxBackups = defaultMaxBackups
	}
	maxAge := cfg.MaxAgeDays
	if maxAge == 0 {
		maxAge = defaultMaxAgeDays
	}

	handlerOpts := &slog.HandlerOptions{Level: slog.LevelInfo}
	handlers := []slog.Handler{slog.NewTextHandler(stdout, handlerOpts)}

	var lj *lumberjack.Logger
	if mkErr := os.MkdirAll(dir, 0o755); mkErr != nil {
		// Stderr warning, not slog — slog isn't wired yet, and we don't
		// want the warning to recurse into the broken sink.
		fmt.Fprintf(os.Stderr,
			"yaaos-agent: log directory %q unwritable (%v); continuing with stdout-only\n",
			dir, mkErr)
	} else {
		lj = &lumberjack.Logger{
			Filename:   filepath.Join(dir, logFileName),
			MaxSize:    maxSize,
			MaxBackups: maxBackups,
			MaxAge:     maxAge,
			Compress:   true,
		}
		handlers = append(handlers, slog.NewTextHandler(lj, handlerOpts))
	}

	handlers = append(handlers, cfg.ExtraHandlers...)
	slog.SetDefault(slog.New(&multiHandler{handlers: handlers}))

	slog.Info("agent.logging.initialized",
		"dir", dir,
		"file_sink_active", lj != nil,
		"max_size_mb", maxSize,
		"max_backups", maxBackups,
		"max_age_days", maxAge,
		"extra_handlers", len(cfg.ExtraHandlers),
	)

	return func(_ context.Context) error {
		if lj != nil {
			return lj.Close()
		}
		return nil
	}, nil
}

// multiHandler fans an slog.Record out to every wrapped handler.
// Errors from individual handlers are collected; the first non-nil
// error is returned, but every handler still gets a chance to write.
type multiHandler struct {
	handlers []slog.Handler
}

func (m *multiHandler) Enabled(ctx context.Context, level slog.Level) bool {
	for _, h := range m.handlers {
		if h.Enabled(ctx, level) {
			return true
		}
	}
	return false
}

func (m *multiHandler) Handle(ctx context.Context, r slog.Record) error {
	var firstErr error
	for _, h := range m.handlers {
		if !h.Enabled(ctx, r.Level) {
			continue
		}
		if err := h.Handle(ctx, r.Clone()); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func (m *multiHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	next := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		next[i] = h.WithAttrs(attrs)
	}
	return &multiHandler{handlers: next}
}

func (m *multiHandler) WithGroup(name string) slog.Handler {
	next := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		next[i] = h.WithGroup(name)
	}
	return &multiHandler{handlers: next}
}
