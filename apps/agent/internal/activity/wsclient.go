package activity

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/coder/websocket"
)

// WSConn is the dumb transport wrapper around a single bidirectional
// WebSocket connection to the backend's `/api/v1/agents/{id}/activity`
// endpoint. It does not own the protocol — read/write are byte-oriented.
// Compose with `Conductor` for the activity-batching protocol layer.
//
// Reconnect / retry policy lives in the caller; if Read or Send returns
// an error the connection is unusable and Close should be called before
// dialing again.
type WSConn struct {
	conn *websocket.Conn
}

// Dial opens a WebSocket connection to `wsURL` (scheme `ws://` or
// `wss://`) with `Authorization: Bearer <bearer>`. The handshake honors
// `ctx` for timeouts; once it returns, the connection's lifetime is
// independent of `ctx`.
func Dial(ctx context.Context, wsURL, bearer string) (*WSConn, error) {
	if bearer == "" {
		return nil, errors.New("activity: Dial requires a non-empty bearer token")
	}
	headers := http.Header{}
	headers.Set("Authorization", "Bearer "+bearer)
	conn, resp, err := websocket.Dial(ctx, wsURL, &websocket.DialOptions{
		HTTPHeader: headers,
	})
	if err != nil {
		// coder/websocket closes the response body on success; on error
		// the body may still be open with the failure payload.
		if resp != nil && resp.Body != nil {
			_ = resp.Body.Close()
		}
		return nil, fmt.Errorf("activity: WS dial %s: %w", wsURL, err)
	}
	return &WSConn{conn: conn}, nil
}

// Read blocks until one frame arrives or `ctx` is cancelled / the
// connection closes. Returns the raw frame bytes.
func (w *WSConn) Read(ctx context.Context) ([]byte, error) {
	_, raw, err := w.conn.Read(ctx)
	return raw, err
}

// Send writes one frame as a text WS message. Used as the Conductor's
// SendFunc — the Conductor never panics on Send errors; it logs and
// drops, leaving reconnect to the caller. Uses a short bounded
// context so a wedged peer can't permanently block a flush goroutine.
func (w *WSConn) Send(frame []byte) error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return w.conn.Write(ctx, websocket.MessageText, frame)
}

// Close ends the connection with a normal closure status. Idempotent.
func (w *WSConn) Close() error {
	return w.conn.Close(websocket.StatusNormalClosure, "")
}

// RunInbound is the read-loop helper: pulls frames off `conn` and
// dispatches each into `cond.HandleInbound`. Returns when ctx is
// cancelled or the connection errors. Malformed frames are logged
// inside the Conductor and the loop continues — one bad frame doesn't
// kill the WS.
func RunInbound(ctx context.Context, conn *WSConn, cond *Conductor) error {
	for {
		raw, err := conn.Read(ctx)
		if err != nil {
			return err
		}
		if err := cond.HandleInbound(raw); err != nil {
			// Log and keep going. The Conductor's own logging is the
			// canonical observability hook; suppressing here keeps the
			// loop alive.
			continue
		}
	}
}
