// yaaos WorkspaceAgent — customer-deployed Go binary.
//
// Subcommands:
//
//	agent supervisor     — long-poll the control plane, dispatch
//	                       AgentCommands, heartbeat. Phase 6 ships the
//	                       skeleton; real workspace spawning lands in a
//	                       follow-on iteration.
//	agent workspace      — per-workspace child process. Phase 6 ships the
//	                       package stub; the subcommand entry currently
//	                       prints a marker so integration smoke tests
//	                       observe it (full impl: follow-on).
//
// Zero business logic — every threshold, prompt, lesson, depth, timeout
// comes from the control plane via payload.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/supervisor"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: agent <supervisor|workspace>")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "supervisor":
		if err := runSupervisor(); err != nil {
			log.Fatalf("supervisor: %v", err)
		}
	case "workspace":
		// Phase 6 foundations: stub. Full body lands alongside the workspace
		// subprocess management work (IPC reader/writer, repo clone, Claude
		// Code invocation, cleanup).
		fmt.Fprintln(os.Stderr, "workspace subcommand: not implemented (Phase 6 follow-on)")
		os.Exit(1)
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", os.Args[1])
		os.Exit(2)
	}
}

func runSupervisor() error {
	cfg := supervisor.Config{
		BaseURL:          envOr("YAAOS_BACKEND_URL", "http://localhost:8080"),
		AgentPodID:       envOr("YAAOS_AGENT_POD_ID", randomPodID()),
		Version:          envOr("YAAOS_AGENT_VERSION", "0.0.0-dev"),
		SignedSTSRequest: envOr("YAAOS_SIGNED_STS_REQUEST", "placeholder-phase-7-wires-real-sts"),
	}
	httpClient := &http.Client{Timeout: 0} // no global timeout — long-poll needs to wait
	cli := protocol.NewClient(cfg.BaseURL, httpClient)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	sup := supervisor.New(cfg, cli, stderrLogger{})
	log.Printf("supervisor.starting backend=%s pod=%s", cfg.BaseURL, cfg.AgentPodID)
	if err := sup.Run(ctx); err != nil {
		return err
	}
	log.Printf("supervisor.stopped")
	return nil
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

type stderrLogger struct{}

func (stderrLogger) Info(msg string, kv ...any)  { log.Printf("INFO  %s %s", msg, kvString(kv)) }
func (stderrLogger) Warn(msg string, kv ...any)  { log.Printf("WARN  %s %s", msg, kvString(kv)) }
func (stderrLogger) Error(msg string, kv ...any) { log.Printf("ERROR %s %s", msg, kvString(kv)) }

func kvString(kv []any) string {
	if len(kv) == 0 {
		return ""
	}
	s := ""
	for i := 0; i+1 < len(kv); i += 2 {
		if i > 0 {
			s += " "
		}
		s += fmt.Sprintf("%v=%v", kv[i], kv[i+1])
	}
	return s
}

// randomPodID returns a 32-hex-char string — sufficient for the per-pod
// identifier the backend uses to dedup heartbeats. The backend treats it
// as opaque; full UUID-v4 conformance isn't required.
func randomPodID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}
