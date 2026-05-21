// Package ipc handles JSON-newline framing for supervisor↔workspace pipes.
//
// The agent's supervisor process and each workspace child process exchange
// messages over stdin/stdout pipes. Frames are newline-terminated JSON
// objects ("ndjson"). Each frame carries a `kind` discriminator the
// receiver inspects to route into a typed handler.
//
// The encoder enforces the contract: callers hand in any JSON-marshallable
// value, the encoder serializes + appends '\n' + writes atomically per
// frame. The decoder tolerates partial reads from the underlying pipe and
// emits one decoded value per call.
//
// Error envelopes — when a workspace process fails to handle a message it
// emits an `{"kind":"error","message":...,"detail":...}` frame instead of
// crashing. Callers wrap their own typed events around that shape.
package ipc

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"
)

// ErrClosed is returned by the decoder when the underlying reader has hit
// EOF cleanly (no partial frame buffered).
var ErrClosed = errors.New("ipc: stream closed")

// Encoder serializes typed messages onto an io.Writer as newline-terminated
// JSON. Safe for concurrent use — one goroutine can format an event while
// another writes a status reply.
type Encoder struct {
	mu sync.Mutex
	w  io.Writer
}

// NewEncoder returns an Encoder that writes to `w`.
func NewEncoder(w io.Writer) *Encoder {
	return &Encoder{w: w}
}

// Write marshals `v` to JSON and writes a single newline-terminated frame.
// The marshal + write are done under a lock so interleaved Encode calls
// from multiple goroutines never produce corrupted frames.
func (e *Encoder) Write(v any) error {
	if e == nil || e.w == nil {
		return errors.New("ipc: nil encoder")
	}
	buf, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("ipc: marshal: %w", err)
	}
	buf = append(buf, '\n')
	e.mu.Lock()
	defer e.mu.Unlock()
	_, err = e.w.Write(buf)
	return err
}

// Decoder reads newline-terminated JSON frames from an io.Reader. The
// underlying bufio.Scanner buffers partial frames so a short read on the
// pipe doesn't truncate a message.
type Decoder struct {
	scanner *bufio.Scanner
}

// NewDecoder wraps `r`. The buffer is sized large enough for typical
// AgentCommand payloads (8 MiB cap matches the spec's request-body limit).
func NewDecoder(r io.Reader) *Decoder {
	s := bufio.NewScanner(r)
	s.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	return &Decoder{scanner: s}
}

// Read decodes the next frame into `v`. Returns ErrClosed when the stream
// ends without a partial frame buffered. Other errors include malformed
// JSON and frames that exceed the buffer cap.
func (d *Decoder) Read(v any) error {
	if !d.scanner.Scan() {
		if err := d.scanner.Err(); err != nil {
			return fmt.Errorf("ipc: scan: %w", err)
		}
		return ErrClosed
	}
	line := d.scanner.Bytes()
	if len(line) == 0 {
		// Blank line — caller may want to treat as a no-op; signal EOF-like.
		return ErrClosed
	}
	if err := json.Unmarshal(line, v); err != nil {
		return fmt.Errorf("ipc: unmarshal: %w", err)
	}
	return nil
}

// ErrorFrame is the wire shape every typed frame is expected to fall back
// to when the workspace process fails to act on a request. Receivers
// looking for typed messages can probe for `kind == "error"` and decode
// into this shape to surface the failure to the supervisor.
type ErrorFrame struct {
	Kind    string `json:"kind"` // always "error"
	Message string `json:"message"`
	Detail  string `json:"detail,omitempty"`
}

// NewErrorFrame constructs an error frame with the conventional `kind`.
func NewErrorFrame(message, detail string) ErrorFrame {
	return ErrorFrame{Kind: "error", Message: message, Detail: detail}
}
