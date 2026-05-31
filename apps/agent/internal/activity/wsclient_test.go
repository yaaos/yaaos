package activity

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/yaaos/agent/internal/protocol"
)

// fakeServer accepts WS connections, records the Authorization header,
// and exposes channels to inject frames or read inbound frames.
type fakeServer struct {
	URL    string
	AuthCh chan string // captured Authorization header value, one per Accept
	server *httptest.Server

	mu       sync.Mutex
	conns    []*websocket.Conn // one per accepted connection
	inbound  [][]byte          // frames the server received from the client
	inboundC chan []byte       // signal channel (one signal per received frame)
}

func newFakeServer() *fakeServer {
	fs := &fakeServer{
		AuthCh:   make(chan string, 4),
		inboundC: make(chan []byte, 16),
	}
	fs.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		c, err := websocket.Accept(w, r, nil)
		if err != nil {
			return
		}
		fs.mu.Lock()
		fs.conns = append(fs.conns, c)
		fs.mu.Unlock()
		// Signal *after* the connection is registered so tests that wait
		// on AuthCh and then call pushFromServer don't race the append.
		fs.AuthCh <- auth
		// Read loop until the client disconnects. Captured frames go
		// into fs.inbound + fs.inboundC.
		ctx := r.Context()
		for {
			_, raw, err := c.Read(ctx)
			if err != nil {
				return
			}
			fs.mu.Lock()
			cp := make([]byte, len(raw))
			copy(cp, raw)
			fs.inbound = append(fs.inbound, cp)
			fs.mu.Unlock()
			select {
			case fs.inboundC <- cp:
			default:
			}
		}
	}))
	// httptest.NewServer URL is http://... — switch to ws:// for Dial.
	fs.URL = strings.Replace(fs.server.URL, "http://", "ws://", 1)
	return fs
}

func (fs *fakeServer) Close() {
	fs.mu.Lock()
	for _, c := range fs.conns {
		_ = c.Close(websocket.StatusNormalClosure, "")
	}
	fs.mu.Unlock()
	fs.server.Close()
}

// pushFromServer writes a frame to the most recently accepted client.
func (fs *fakeServer) pushFromServer(t *testing.T, frame []byte) {
	t.Helper()
	fs.mu.Lock()
	defer fs.mu.Unlock()
	if len(fs.conns) == 0 {
		t.Fatal("no accepted connections to push from")
	}
	c := fs.conns[len(fs.conns)-1]
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := c.Write(ctx, websocket.MessageText, frame); err != nil {
		t.Fatalf("server push: %v", err)
	}
}

func TestWSConn_DialIncludesBearerHeader(t *testing.T) {
	fs := newFakeServer()
	defer fs.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	conn, err := Dial(ctx, fs.URL, "test-bearer")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()

	select {
	case auth := <-fs.AuthCh:
		if auth != "Bearer test-bearer" {
			t.Errorf("Authorization header: got %q want %q", auth, "Bearer test-bearer")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("server never received the WS upgrade")
	}
}

func TestWSConn_SendReachesServer(t *testing.T) {
	fs := newFakeServer()
	defer fs.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	conn, err := Dial(ctx, fs.URL, "test-bearer")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()

	if err := conn.Send([]byte(`{"hello":"world"}`)); err != nil {
		t.Fatalf("Send: %v", err)
	}
	select {
	case got := <-fs.inboundC:
		if string(got) != `{"hello":"world"}` {
			t.Errorf("server got %q", string(got))
		}
	case <-time.After(2 * time.Second):
		t.Fatal("server never received the frame")
	}
}

func TestWSConn_ReadDeliversServerFrames(t *testing.T) {
	fs := newFakeServer()
	defer fs.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	conn, err := Dial(ctx, fs.URL, "test-bearer")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()

	// Wait until the server has accepted the connection (signaled via AuthCh).
	<-fs.AuthCh
	// Server pushes a frame; client should read it.
	fs.pushFromServer(t, []byte(`{"type":"subscribe","workspace_id":"ws-1","workflow_execution_id":"wf-1"}`))

	raw, err := conn.Read(ctx)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if !strings.Contains(string(raw), `"workspace_id":"ws-1"`) {
		t.Errorf("Read got %q", string(raw))
	}
}

func TestRunInbound_FeedsConductor(t *testing.T) {
	// End-to-end producer side: server pushes subscribe → RunInbound
	// updates Conductor → Publish goes through → server receives the
	// encoded activity_batch frame.
	fs := newFakeServer()
	defer fs.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	conn, err := Dial(ctx, fs.URL, "test-bearer")
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()

	<-fs.AuthCh
	cond := NewConductor(20*time.Millisecond, conn.Send)
	cond.Start(ctx)
	defer cond.Stop()

	// Spawn the read-loop.
	done := make(chan error, 1)
	go func() { done <- RunInbound(ctx, conn, cond) }()

	// Server sends subscribe → client should now route Publish through.
	fs.pushFromServer(t, []byte(`{"type":"subscribe","workspace_id":"ws-1","workflow_execution_id":"wf-1"}`))
	// Wait for the subscribe to propagate.
	deadline := time.Now().Add(2 * time.Second)
	for !cond.subs.Contains("ws-1") && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond) // reason: WS read goroutine blocks on OS network I/O (httptest.Server); not durably blocked in synctest sense.
	}
	if !cond.subs.Contains("ws-1") {
		t.Fatal("subscribe never reached the Conductor")
	}

	cond.Publish("ws-1", protocol.AgentEvent{CommandID: "c-1", Kind: protocol.EventProgress})

	select {
	case got := <-fs.inboundC:
		// Skip any non-batch noise (we know the test inputs none, but
		// be defensive).
		if !strings.Contains(string(got), `"activity_batch"`) {
			t.Errorf("server got non-batch frame: %s", string(got))
		}
		if !strings.Contains(string(got), `"workflow_execution_id":"wf-1"`) {
			t.Errorf("frame missing wf-1: %s", string(got))
		}
	case <-time.After(2 * time.Second):
		t.Fatal("server never received the activity_batch")
	}

	// Tear down the read-loop by cancelling ctx.
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("RunInbound did not exit on ctx cancel")
	}
}
