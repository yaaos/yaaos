package supervisor

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/yaaos/agent/internal/identity"
	"github.com/yaaos/agent/internal/protocol"
)

// noopProvider is a test-only Provider that returns empty credentials.
// Used by tests that exercise non-identity code paths (e.g., WS wiring)
// and never call Run.
type noopProvider struct{}

func (noopProvider) Exchange(_ context.Context) (identity.Credentials, error) {
	return identity.Credentials{}, nil
}

// fakeActivityServer accepts one WS upgrade, captures the bearer header,
// and records inbound activity_batch frames. Mirrors the test fixture
// from internal/activity but is intentionally redeclared here to keep
// the supervisor package self-contained.
type fakeActivityServer struct {
	URL       string
	AuthCh    chan string
	server    *httptest.Server
	mu        sync.Mutex
	conns     []*websocket.Conn
	inboundCh chan []byte
}

func newFakeActivityServer() *fakeActivityServer {
	fs := &fakeActivityServer{
		AuthCh:    make(chan string, 4),
		inboundCh: make(chan []byte, 16),
	}
	fs.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fs.AuthCh <- r.Header.Get("Authorization")
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		fs.mu.Lock()
		fs.conns = append(fs.conns, c)
		fs.mu.Unlock()
		ctx := r.Context()
		for {
			_, raw, err := c.Read(ctx)
			if err != nil {
				return
			}
			select {
			case fs.inboundCh <- raw:
			default:
			}
		}
	}))
	fs.URL = strings.Replace(fs.server.URL, "http://", "ws://", 1)
	return fs
}

func (fs *fakeActivityServer) Close() {
	fs.mu.Lock()
	for _, c := range fs.conns {
		_ = c.Close(websocket.StatusNormalClosure, "")
	}
	fs.mu.Unlock()
	fs.server.Close()
}

func (fs *fakeActivityServer) push(t *testing.T, frame []byte) {
	t.Helper()
	fs.mu.Lock()
	defer fs.mu.Unlock()
	if len(fs.conns) == 0 {
		t.Fatal("no accepted connections")
	}
	c := fs.conns[len(fs.conns)-1]
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := c.Write(ctx, websocket.MessageText, frame); err != nil {
		t.Fatalf("server push: %v", err)
	}
}

func TestSupervisor_ActivityWS_ProgressEventsRouteThroughConductor(t *testing.T) {
	// End-to-end wiring proof: configure cfg.ActivityWSURL, the
	// supervisor dials + runs the read-loop + creates a Conductor.
	// After the server pushes `subscribe`, calling the supervisor's
	// internal progressForwarder (via dispatching a command) should
	// produce an outbound activity_batch frame on the WS.
	fs := newFakeActivityServer()
	defer fs.Close()

	// We don't run the full Supervisor.Run loop (it'd need an HTTP
	// backend for identity / claim / heartbeat). Instead, exercise
	// setupActivityWS directly + invoke the routing logic by hand.
	s := New(Config{
		BaseURL:               "http://unused",
		AgentPodID:            "pod-1",
		Version:               "test",
		ActivityWSURL:         fs.URL,
		ActivityBatchInterval: 20 * time.Millisecond,
	}, protocol.NewClient("http://unused", nil), nil, noopProvider{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s.setupActivityWS(ctx, "test-bearer")
	defer func() {
		if s.conductor != nil {
			s.conductor.Stop()
		}
		if s.wsConn != nil {
			_ = s.wsConn.Close()
		}
	}()
	if s.conductor == nil || s.wsConn == nil {
		t.Fatal("setupActivityWS should have populated conductor + wsConn")
	}

	// Server received the dial — assert auth header.
	select {
	case auth := <-fs.AuthCh:
		if auth != "Bearer test-bearer" {
			t.Errorf("auth header: got %q", auth)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("server never received the WS upgrade")
	}

	// Server pushes subscribe → wait for Conductor to apply it.
	fs.push(t, []byte(`{"type":"subscribe","workspace_id":"ws-1","workflow_execution_id":"wf-1"}`))
	deadline := time.Now().Add(2 * time.Second)
	for !s.conductor.IsSubscribed("ws-1") && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond) // reason: WS read goroutine blocks on OS network I/O (httptest.Server); not durably blocked in synctest sense.
	}
	if !s.conductor.IsSubscribed("ws-1") {
		t.Fatal("subscribe never propagated into the Conductor")
	}

	// Now publish a progress event the way routeCommand would (skipping
	// the actual dispatch since we're testing the wire, not the pool).
	s.conductor.Publish("ws-1", protocol.AgentEvent{
		CommandID: "c-1",
		Kind:      protocol.EventProgress,
	})

	select {
	case got := <-fs.inboundCh:
		if !strings.Contains(string(got), `"activity_batch"`) {
			t.Errorf("server got non-batch frame: %s", string(got))
		}
		if !strings.Contains(string(got), `"workflow_execution_id":"wf-1"`) {
			t.Errorf("frame missing wf-1: %s", string(got))
		}
	case <-time.After(2 * time.Second):
		t.Fatal("server never received the activity_batch")
	}
}

func TestSupervisor_ActivityWS_DialFailureDoesNotPopulateConductor(t *testing.T) {
	// Set ActivityWSURL to an unreachable address. setupActivityWS
	// should log and leave conductor/wsConn nil so progressForwarder
	// falls back to the HTTP path.
	s := New(Config{
		BaseURL:       "http://unused",
		AgentPodID:    "pod-1",
		Version:       "test",
		ActivityWSURL: "ws://127.0.0.1:1/never-listens",
	}, protocol.NewClient("http://unused", nil), nil, noopProvider{})
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	s.setupActivityWS(ctx, "test-bearer")
	if s.conductor != nil {
		t.Error("dial failure should leave conductor nil")
	}
	if s.wsConn != nil {
		t.Error("dial failure should leave wsConn nil")
	}
}
