package protocol

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"
)

// ErrNoCommand is returned by Client.ClaimCommand when the long-poll
// timed out without work — the agent re-arms the poll.
var ErrNoCommand = errors.New("protocol: no command available (204)")

// ErrStaleClaim is returned by Client.PostCommandEvent when the backend
// responds with HTTP 410 Gone — the command row no longer exists (the
// claim was retired). The agent drops the event without retry; the
// backend's failsafe owns in-flight recovery.
var ErrStaleClaim = errors.New("protocol: stale claim (410)")

// CommandEventAck is the response body returned by POST /api/v1/commands/{id}/events.
// The backend returns 200 on success; 410 signals a stale claim (ErrStaleClaim).
type CommandEventAck struct {
	Outcome string `json:"command_event_outcome"`
}

// CommandEventOutcomeRecorded is the outcome value when the event was
// persisted and any workflow side-effects fired.
const CommandEventOutcomeRecorded = "event_recorded"

// Client is the HTTP client wrapper for the 5 backend endpoints. Safe
// for concurrent use — http.Client itself is concurrency-safe.
type Client struct {
	baseURL string
	bearer  string
	http    *http.Client
}

// NewClient returns a Client targeting `baseURL` (e.g. "https://yaaos.example.com").
// `httpClient` may be nil — a default `&http.Client{}` with no global timeout
// is used so long-poll requests aren't cut off; per-call timeouts are
// supplied via the request context.
func NewClient(baseURL string, httpClient *http.Client) *Client {
	if httpClient == nil {
		httpClient = &http.Client{}
	}
	return &Client{baseURL: baseURL, http: httpClient}
}

// SetBearer installs the bearer that subsequent calls send in the
// Authorization header. Pass empty to clear (only the identity-exchange
// endpoint accepts an unauthenticated call).
func (c *Client) SetBearer(b string) { c.bearer = b }

// ExchangeIdentity POSTs the signed-STS payload and returns the bearer
// + agent_id + instance_id. On success the bearer is NOT stored — caller decides.
func (c *Client) ExchangeIdentity(ctx context.Context, req IdentityExchangeRequest) (*IdentityExchangeResponse, error) {
	var resp IdentityExchangeResponse
	if err := c.doJSON(ctx, http.MethodPost, "/api/v1/agent/identity", req, &resp, false); err != nil {
		return nil, err
	}
	return &resp, nil
}

// Heartbeat reports liveness + workspace inventory. Agent identity is
// derived from the bearer — no agent ID in the URL.
func (c *Client) Heartbeat(ctx context.Context, req HeartbeatRequest) (*HeartbeatResponse, error) {
	var resp HeartbeatResponse
	if err := c.doJSON(ctx, http.MethodPost, "/api/v1/agent/heartbeat", req, &resp, true); err != nil {
		return nil, err
	}
	return &resp, nil
}

// ClaimCommand long-polls for the next command. Returns the raw JSON bytes
// on success; the caller passes these to command.Decode for typed dispatch.
// Returns ErrNoCommand when the backend responds 204. Agent identity is
// derived from the bearer — no agent ID in the URL.
func (c *Client) ClaimCommand(ctx context.Context, req ClaimRequest) ([]byte, error) {
	path := "/api/v1/agent/commands/claim"
	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal: %w", err)
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	c.applyBearer(httpReq)
	httpResp, err := c.http.Do(httpReq)
	if err != nil {
		return nil, err
	}
	defer func() { _ = httpResp.Body.Close() }()

	switch httpResp.StatusCode {
	case http.StatusOK:
		raw, err := io.ReadAll(httpResp.Body)
		if err != nil {
			return nil, fmt.Errorf("read command body: %w", err)
		}
		return raw, nil
	case http.StatusNoContent:
		return nil, ErrNoCommand
	case http.StatusUnauthorized:
		return nil, fmt.Errorf("claim: unauthorized")
	default:
		return nil, fmt.Errorf("claim: unexpected status %d", httpResp.StatusCode)
	}
}

// PostCommandEvent reports progress or terminal outcome for an AgentCommand.
// Returns (ack, nil) on 200; returns (nil, ErrStaleClaim) on 410 (command row
// retired — the claim is stale); returns (nil, err) on any other failure.
func (c *Client) PostCommandEvent(ctx context.Context, commandID string, event AgentEvent) (*CommandEventAck, error) {
	path := fmt.Sprintf("/api/v1/commands/%s/events", commandID)
	if event.ReportedAt.IsZero() {
		event.ReportedAt = time.Now().UTC()
	}
	var ack CommandEventAck
	if err := c.doJSON(ctx, http.MethodPost, path, event, &ack, true); err != nil {
		return nil, err
	}
	return &ack, nil
}

// doJSON is the generic POST helper. `out` may be nil for endpoints that
// don't return a typed body. `withBearer=false` is only used by the
// identity-exchange path which is unauthenticated.
func (c *Client) doJSON(ctx context.Context, method, path string, in, out any, withBearer bool) error {
	var body io.Reader
	if in != nil {
		buf, err := json.Marshal(in)
		if err != nil {
			return fmt.Errorf("marshal: %w", err)
		}
		body = bytes.NewReader(buf)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, body)
	if err != nil {
		return err
	}
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if withBearer {
		c.applyBearer(req)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()

	switch resp.StatusCode {
	case http.StatusOK, http.StatusNoContent:
		if out == nil || resp.StatusCode == http.StatusNoContent {
			return nil
		}
		return json.NewDecoder(resp.Body).Decode(out)
	case http.StatusUnauthorized:
		return fmt.Errorf("%s %s: unauthorized", method, path)
	case http.StatusGone:
		// 410 Gone — the backend retired the command row (stale claim).
		// Return the typed sentinel so callers can distinguish this from
		// transient errors without string-matching.
		return fmt.Errorf("%w", ErrStaleClaim)
	default:
		raw, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("%s %s: %d %s", method, path, resp.StatusCode, string(raw))
	}
}

// Deregister sends DELETE /api/v1/agent/identity — the graceful-shutdown
// "going away" signal. The control plane eagerly marks the agent offline,
// revokes the bearer, and expires any held workspaces. Best-effort: errors
// are returned but the caller (supervisor shutdown) always continues.
func (c *Client) Deregister(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, c.baseURL+"/api/v1/agent/identity", nil)
	if err != nil {
		return err
	}
	c.applyBearer(req)
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusNoContent {
		raw, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("deregister: unexpected status %d: %s", resp.StatusCode, string(raw))
	}
	return nil
}

func (c *Client) applyBearer(req *http.Request) {
	if c.bearer != "" {
		req.Header.Set("Authorization", "Bearer "+c.bearer)
	}
}
