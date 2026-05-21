package ipc

import (
	"bytes"
	"encoding/json"
	"io"
	"strings"
	"sync"
	"testing"
)

func TestEncoderWritesNewlineTerminatedFrame(t *testing.T) {
	var buf bytes.Buffer
	enc := NewEncoder(&buf)
	if err := enc.Write(map[string]any{"kind": "hello", "n": 1}); err != nil {
		t.Fatalf("encode: %v", err)
	}
	got := buf.String()
	if !strings.HasSuffix(got, "\n") {
		t.Fatalf("expected newline terminator, got %q", got)
	}
	// Each line should be valid JSON.
	var decoded map[string]any
	if err := json.Unmarshal([]byte(strings.TrimRight(got, "\n")), &decoded); err != nil {
		t.Fatalf("encoded frame is not valid JSON: %v", err)
	}
	if decoded["kind"] != "hello" {
		t.Fatalf("decoded kind = %v", decoded["kind"])
	}
}

func TestDecoderReadsOneFramePerCall(t *testing.T) {
	src := strings.NewReader(`{"kind":"a","x":1}` + "\n" + `{"kind":"b","x":2}` + "\n")
	dec := NewDecoder(src)

	var first map[string]any
	if err := dec.Read(&first); err != nil {
		t.Fatalf("first read: %v", err)
	}
	if first["kind"] != "a" {
		t.Fatalf("first kind = %v", first["kind"])
	}

	var second map[string]any
	if err := dec.Read(&second); err != nil {
		t.Fatalf("second read: %v", err)
	}
	if second["kind"] != "b" {
		t.Fatalf("second kind = %v", second["kind"])
	}

	var third map[string]any
	if err := dec.Read(&third); err != ErrClosed {
		t.Fatalf("third read expected ErrClosed, got %v", err)
	}
}

// slowReader emits one byte per Read so the decoder must buffer partial
// frames across many calls.
type slowReader struct {
	data []byte
	pos  int
}

func (s *slowReader) Read(p []byte) (int, error) {
	if s.pos >= len(s.data) {
		return 0, io.EOF
	}
	p[0] = s.data[s.pos]
	s.pos++
	return 1, nil
}

func TestDecoderTolerantsPartialReads(t *testing.T) {
	payload := `{"kind":"big","value":"abcdefghijklmnopqrstuvwxyz"}` + "\n"
	dec := NewDecoder(&slowReader{data: []byte(payload)})

	var got map[string]any
	if err := dec.Read(&got); err != nil {
		t.Fatalf("byte-at-a-time read: %v", err)
	}
	if got["kind"] != "big" || got["value"] != "abcdefghijklmnopqrstuvwxyz" {
		t.Fatalf("unexpected frame: %+v", got)
	}
}

func TestDecoderRejectsMalformedJSON(t *testing.T) {
	src := strings.NewReader("{this-is-not-json}\n")
	dec := NewDecoder(src)
	var got map[string]any
	if err := dec.Read(&got); err == nil {
		t.Fatal("expected unmarshal error, got nil")
	}
}

func TestEncoderConcurrentWritesAreFrameSafe(t *testing.T) {
	var buf bytes.Buffer
	enc := NewEncoder(&buf)

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_ = enc.Write(map[string]any{"i": i})
		}(i)
	}
	wg.Wait()

	// Every line that was written must be parseable JSON.
	lines := strings.Split(strings.TrimRight(buf.String(), "\n"), "\n")
	if len(lines) != 50 {
		t.Fatalf("expected 50 frames, got %d", len(lines))
	}
	for _, line := range lines {
		var v map[string]any
		if err := json.Unmarshal([]byte(line), &v); err != nil {
			t.Fatalf("interleaved write produced corrupt frame %q: %v", line, err)
		}
	}
}

func TestErrorFrameShape(t *testing.T) {
	frame := NewErrorFrame("boom", "stack")
	if frame.Kind != "error" || frame.Message != "boom" || frame.Detail != "stack" {
		t.Fatalf("unexpected error frame: %+v", frame)
	}
}
