package activity

import (
	"context"
	"log"
	"time"

	"github.com/yaaos/agent/internal/protocol"
)

// SendFunc writes one outbound JSON frame onto the WebSocket. The
// Conductor doesn't own the WS — it's parameterized via this callback
// so the WS dial/auth/reconnect strategy lives in the caller.
type SendFunc func(frame []byte) error

// Conductor composes SubscriptionSet + WorkspaceMapping + Batcher into
// the single consumer-facing API the WS-client uses:
//
//   - HandleInbound(raw): decode one inbound `subscribe`/`unsubscribe`
//     message and apply it to both the SubscriptionSet (filters
//     outbound publishes) and the WorkspaceMapping (translates
//     workspace_id → workflow_execution_id for outbound frames).
//   - Publish(workspaceID, ev): forward an AgentEvent into the
//     Batcher. Drops on the floor if workspace_id isn't currently
//     subscribed.
//
// On each flush tick the Batcher hands each (workspace_id, []events)
// batch back to the Conductor's flush adapter, which looks up the
// matching workflow_execution_id, encodes the activity_batch envelope,
// and calls SendFunc. Missing mapping → drop (defensive; shouldn't
// happen given the slice-79 payload shape).
type Conductor struct {
	subs    *SubscriptionSet
	mapping *WorkspaceMapping
	batcher *Batcher
	send    SendFunc
}

func NewConductor(flushInterval time.Duration, send SendFunc) *Conductor {
	subs := NewSubscriptionSet()
	mapping := NewWorkspaceMapping()
	c := &Conductor{
		subs:    subs,
		mapping: mapping,
		send:    send,
	}
	c.batcher = NewBatcher(subs, flushInterval, c.flushOne)
	return c
}

func (c *Conductor) Start(ctx context.Context) { c.batcher.Start(ctx) }
func (c *Conductor) Stop()                     { c.batcher.Stop() }

// IsSubscribed reports whether the backend has sent a `subscribe` for
// `workspaceID` and not yet sent the matching `unsubscribe`. Exposed
// for tests + diagnostics; consumers typically don't need to check —
// the Conductor filters at Publish time.
func (c *Conductor) IsSubscribed(workspaceID string) bool {
	return c.subs.Contains(workspaceID)
}

// HandleInbound decodes one frame and applies it. Returns the decode
// error so the caller can log / disconnect on malformed traffic.
func (c *Conductor) HandleInbound(raw []byte) error {
	msg, err := DecodeInbound(raw)
	if err != nil {
		return err
	}
	switch msg.Kind {
	case InboundSubscribe:
		// Mapping must precede the SubscriptionSet add — otherwise an
		// in-flight Publish on another goroutine could race and find
		// the workspace subscribed but unmapped.
		if msg.WorkflowExecutionID != "" {
			c.mapping.Set(msg.WorkspaceID, msg.WorkflowExecutionID)
		}
		c.subs.Add(msg.WorkspaceID)
	case InboundUnsubscribe:
		c.subs.Remove(msg.WorkspaceID)
		c.mapping.Remove(msg.WorkspaceID)
	}
	return nil
}

// Publish forwards an event to the Batcher. Unsubscribed workspaces
// drop at the Batcher's gate; this method never blocks on the WS.
func (c *Conductor) Publish(workspaceID string, ev protocol.AgentEvent) {
	c.batcher.Publish(workspaceID, ev)
}

// flushOne is the Batcher's FlushFunc adapter. Resolves the workflow
// id from the cached mapping, encodes one activity_batch envelope,
// hands it to SendFunc. Send errors are logged and dropped — the
// caller's transport layer owns retry / reconnect.
func (c *Conductor) flushOne(workspaceID string, events []protocol.AgentEvent) {
	wf, ok := c.mapping.Get(workspaceID)
	if !ok {
		log.Printf("activity: drop batch — no workflow_execution_id mapping for workspace %s", workspaceID)
		return
	}
	frame, err := EncodeBatch(wf, events)
	if err != nil {
		log.Printf("activity: encode batch failed for %s: %v", wf, err)
		return
	}
	if err := c.send(frame); err != nil {
		log.Printf("activity: send batch failed for %s: %v", wf, err)
	}
}
