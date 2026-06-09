// yaaos WorkspaceAgent — customer-deployed Go binary.
//
// Subcommands:
//
//	agent supervisor     — long-poll the control plane, dispatch
//	                       AgentCommands, heartbeat.
//	agent workspace      — per-workspace child process running the IPC
//	                       dispatcher (stdin → commands, stdout → events).
//
// Zero business logic — every threshold, prompt, lesson, depth, timeout
// comes from the control plane via payload.
package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/yaaos/agent/internal/identity"
	"github.com/yaaos/agent/internal/logging"
	"github.com/yaaos/agent/internal/observability"
	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/supervisor"
	"github.com/yaaos/agent/internal/workspace"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
)

const agentVersion = "0.0.1"

// Hardcoded production backend. Customers don't configure this — the
// agent ships pre-pointed at app.yaaos.cloud. Overrideable via
// `YAAOS_BACKEND_URL` for development and integration testing only.
const defaultBackendURL = "https://app.yaaos.cloud"

func main() {
	// run() returns the desired exit code so deferred cleanups (the log
	// file flush in particular) actually fire before exit. os.Exit
	// bypasses defers, so confine it to one spot here.
	os.Exit(run())
}

func run() int {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: agent <supervisor|workspace>")
		return 2
	}

	// Wire OTel first (if configured), then slog with the OTel bridge
	// as a third sink. OTel must initialize before logging so we have
	// the slog handler ready to install on the fan-out.
	//
	// The `workspace` subcommand uses stdout as its IPC event pipe back
	// to the supervisor (one JSON frame per line). Logs MUST NOT touch
	// stdout in that mode or they'll be parsed as protocol frames and
	// crash the supervisor. Route the console sink to stderr there;
	// `supervisor` mode keeps stdout for ECS awslogs / CloudWatch.
	consoleWriter := io.Writer(os.Stdout)
	if os.Args[1] == "workspace" {
		consoleWriter = os.Stderr
	}

	otelRes, err := observability.Init(context.Background(), observability.Config{
		ServiceVersion: envOr("YAAOS_AGENT_VERSION", agentVersion),
		// InstanceID is empty at boot; populated via observability.SetInstanceID
		// after identity exchange (before BindExporter runs on ConfigUpdate).
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "observability.init failed: %v\n", err)
		return 1
	}
	defer func() { _ = otelRes.Shutdown(context.Background()) }()

	// Wire the live log bridge into the fan-out once. It stays dormant until
	// OTel is configured (env endpoint here, or a later ConfigUpdate), then
	// exports logs without the fan-out needing to be rebuilt.
	logCfg := logging.Config{StdoutWriter: consoleWriter}
	logCfg.ExtraHandlers = append(logCfg.ExtraHandlers, otelRes.SlogHandler)
	shutdownLogs, err := logging.Init(logCfg)
	if err != nil {
		fmt.Fprintf(os.Stderr, "logging.init failed: %v\n", err)
		return 1
	}
	defer func() { _ = shutdownLogs(context.Background()) }()

	switch os.Args[1] {
	case "supervisor":
		if err := runSupervisor(); err != nil {
			slog.Error("supervisor.fatal", "err", err.Error())
			return 1
		}
	case "workspace":
		if err := runWorkspace(); err != nil {
			slog.Error("workspace.fatal", "err", err.Error())
			return 1
		}
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", os.Args[1])
		return 2
	}
	return 0
}

func runSupervisor() error {
	cfg := supervisor.Config{
		BaseURL:       envOr("YAAOS_BACKEND_URL", defaultBackendURL),
		Version:       envOr("YAAOS_AGENT_VERSION", "0.0.0-dev"),
		WorkspaceRoot: envOr("YAAOS_WORKSPACE_ROOT", ""),
	}
	// NewProvider selects the signing implementation. In production and dev,
	// the aws-sts provider reads IMDS credentials and sigv4-signs a
	// GetCallerIdentity request. In tests, the supervisor can be constructed
	// with a stub provider directly (see supervisor_test.go).
	prov := identity.NewProvider()

	// No global timeout — long-poll needs to wait. Per-call timeouts come
	// from the request context. otelhttp.NewTransport adds a span per
	// outbound HTTP call when OTel is configured; it's transparent when
	// OTel is disabled (no-op tracer).
	httpClient := &http.Client{
		Timeout:   0,
		Transport: otelhttp.NewTransport(http.DefaultTransport),
	}
	cli := protocol.NewClient(cfg.BaseURL, httpClient)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// *slog.Logger satisfies supervisor.Logger directly — Info/Warn/Error
	// signatures match. No adapter needed.
	sup := supervisor.New(cfg, cli, slog.Default(), prov)
	slog.Info("supervisor.starting", "backend", cfg.BaseURL)
	if err := sup.Run(ctx); err != nil {
		return err
	}
	slog.Info("supervisor.stopped")
	return nil
}

func runWorkspace() error {
	// The supervisor spawns this process with stdin = command pipe and
	// stdout = event pipe. Run reads commands, dispatches via the Handler,
	// writes events back.
	//
	// Mounts the RealHandler: tempdir lifecycle + file writes + auth
	// refresh + cleanup all do real work. See workspace/realhandler.go's
	// doc for the scope of ProvisionWorkspace and InvokeClaudeCode.
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	handler := workspace.NewRealHandler(workspace.RealHandlerConfig{
		Root: envOr("YAAOS_WORKSPACE_ROOT", ""),
	})
	slog.Info("workspace.starting")
	if err := workspace.Run(ctx, os.Stdin, os.Stdout, handler, workspace.Options{}); err != nil {
		return err
	}
	slog.Info("workspace.stopped")
	return nil
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
