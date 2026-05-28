package activity

import (
	"encoding/json"
	"fmt"

	"github.com/yaaos/agent/internal/protocol"
)

// InboundKind identifies the message type the backend sends to the
// agent over the activity WebSocket.
type InboundKind string

const (
	InboundSubscribe   InboundKind = "subscribe"
	InboundUnsubscribe InboundKind = "unsubscribe"
)

// InboundMessage is the decoded shape of `subscribe` / `unsubscribe`.
// The backend's SubscriberRegistry ships both `workspace_id` (the
// SubscriptionSet key) and `workflow_execution_id` (the channel key
// the agent needs to write on outbound activity_batch frames) so the
// agent caches the mapping without a backend lookup.
type InboundMessage struct {
	Kind                InboundKind
	WorkspaceID         string
	WorkflowExecutionID string
}

type inboundWire struct {
	Type                string `json:"type"`
	WorkspaceID         string `json:"workspace_id"`
	WorkflowExecutionID string `json:"workflow_execution_id"`
}

// DecodeInbound parses one inbound WS message. Returns an error on
// malformed JSON, unknown `type`, or missing `workspace_id`. The
// `workflow_execution_id` may be empty (older backends),
// in which case the agent can still update the SubscriptionSet but
// can't populate outbound batches — caller decides what to do.
func DecodeInbound(raw []byte) (InboundMessage, error) {
	var w inboundWire
	if err := json.Unmarshal(raw, &w); err != nil {
		return InboundMessage{}, fmt.Errorf("activity: decode inbound: %w", err)
	}
	if w.WorkspaceID == "" {
		return InboundMessage{}, fmt.Errorf("activity: inbound missing workspace_id")
	}
	switch w.Type {
	case "subscribe":
		return InboundMessage{
			Kind:                InboundSubscribe,
			WorkspaceID:         w.WorkspaceID,
			WorkflowExecutionID: w.WorkflowExecutionID,
		}, nil
	case "unsubscribe":
		return InboundMessage{
			Kind:                InboundUnsubscribe,
			WorkspaceID:         w.WorkspaceID,
			WorkflowExecutionID: w.WorkflowExecutionID,
		}, nil
	default:
		return InboundMessage{}, fmt.Errorf("activity: unknown inbound type %q", w.Type)
	}
}

// outboundBatch is the wire envelope for the agent → backend
// `activity_batch` frame. Mirrors the backend handler in
// `apps/backend/app/core/agent_gateway/web.py` (`activity_ws`).
type outboundBatch struct {
	Type                string                `json:"type"`
	WorkflowExecutionID string                `json:"workflow_execution_id"`
	Events              []protocol.AgentEvent `json:"events"`
}

// EncodeBatch produces the JSON bytes for an `activity_batch` frame.
// `events` may be empty — the encoder doesn't filter (the Batcher
// already does that). `workflowExecutionID` is what the backend reads
// to construct `activity:{workflow_execution_id}` channel and fan out
// via core/sse_pubsub.
func EncodeBatch(workflowExecutionID string, events []protocol.AgentEvent) ([]byte, error) {
	return json.Marshal(outboundBatch{
		Type:                "activity_batch",
		WorkflowExecutionID: workflowExecutionID,
		Events:              events,
	})
}
