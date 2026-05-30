package observability

import (
	"context"
	"log/slog"
	"sync/atomic"
)

// liveLogBridge is the slog handler wired into the logging fan-out once, at
// startup, even before OTel is configured. It forwards to the OTel slog bridge
// as soon as a logger provider is installed — by env-var Init or by a late
// ConfigUpdate through BindExporter — and drops records until then.
//
// It exists because the logging fan-out is frozen at logging.Init time: there
// is no way to append a handler later. Wiring this swappable handler at startup
// gives the ConfigUpdate path a seam to light up log export without re-touching
// the fan-out — only the delegate swaps, atomically.
var liveLogBridge = newSwapHandler()

// swapCore holds the atomically swappable delegate shared by a swapHandler and
// every child it derives via WithAttrs / WithGroup.
type swapCore struct {
	delegate atomic.Pointer[slog.Handler]
}

// swapHandler is an slog.Handler that forwards to a delegate installed later.
// WithAttrs / WithGroup calls are recorded and replayed onto the delegate at
// Handle time (preserving their order), so a sub-logger derived before OTel
// binds still carries its attrs and groups once the delegate is set.
type swapHandler struct {
	core *swapCore
	ops  []func(slog.Handler) slog.Handler
}

func newSwapHandler() *swapHandler { return &swapHandler{core: &swapCore{}} }

// setDelegate installs the live handler; pass nil to clear it (tests reset
// between installs).
func (h *swapHandler) setDelegate(d slog.Handler) {
	if d == nil {
		h.core.delegate.Store(nil)
		return
	}
	h.core.delegate.Store(&d)
}

// resolve returns the delegate with this handler's recorded With* chain
// applied, or (nil, false) when no delegate is installed yet.
func (h *swapHandler) resolve() (slog.Handler, bool) {
	d := h.core.delegate.Load()
	if d == nil {
		return nil, false
	}
	cur := *d
	for _, op := range h.ops {
		cur = op(cur)
	}
	return cur, true
}

func (h *swapHandler) Enabled(ctx context.Context, level slog.Level) bool {
	cur, ok := h.resolve()
	if !ok {
		return false
	}
	return cur.Enabled(ctx, level)
}

func (h *swapHandler) Handle(ctx context.Context, r slog.Record) error {
	cur, ok := h.resolve()
	if !ok {
		return nil
	}
	return cur.Handle(ctx, r)
}

func (h *swapHandler) with(op func(slog.Handler) slog.Handler) *swapHandler {
	next := make([]func(slog.Handler) slog.Handler, len(h.ops)+1)
	copy(next, h.ops)
	next[len(h.ops)] = op
	return &swapHandler{core: h.core, ops: next}
}

func (h *swapHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return h.with(func(d slog.Handler) slog.Handler { return d.WithAttrs(attrs) })
}

func (h *swapHandler) WithGroup(name string) slog.Handler {
	return h.with(func(d slog.Handler) slog.Handler { return d.WithGroup(name) })
}
