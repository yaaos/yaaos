// Package identity defines the seam between the supervisor and the mechanism
// that proves this agent pod's identity to the control plane.
//
// The Provider interface abstracts over the exchange protocol so the
// supervisor does not depend on the concrete verification method. Today
// the only implementation is placeholderProvider, which carries a
// pre-signed STS payload. A SigV4-backed implementation can drop in
// later with zero supervisor change.
//
// Credentials returned by Exchange are pinned by the supervisor on the
// first successful call. Subsequent calls (bearer refresh) must return
// the same AgentID and OrgID — a mismatch is an identity-integrity
// violation and the supervisor treats it as fatal.
package identity

import (
	"context"
	"time"
)

// Credentials is the result of a successful identity exchange.
// AgentID and OrgID are assigned by the backend; they are pinned by the
// supervisor on first exchange and must remain stable across renewals.
type Credentials struct {
	// Bearer is the short-lived token used to authenticate every subsequent
	// API call. Returned by the backend on exchange.
	Bearer string

	// ExpiresAt is when Bearer expires. The supervisor refreshes ~1h before
	// this deadline.
	ExpiresAt time.Time

	// AgentID is the per-pod row identifier assigned by the backend.
	// Empty from placeholderProvider — filled in by the supervisor after
	// the HTTP round-trip.
	AgentID string

	// OrgID is the organisation this agent pod belongs to.
	// Empty from placeholderProvider — filled in by the supervisor after
	// the HTTP round-trip.
	OrgID string
}

// Provider issues and renews credentials for this agent pod.
type Provider interface {
	// Exchange contacts the control plane and returns fresh Credentials.
	// Implementations are expected to be idempotent — the supervisor calls
	// Exchange in a retry loop on startup and periodically for bearer
	// renewal.
	Exchange(ctx context.Context) (Credentials, error)
}

// placeholderProvider carries a pre-built signed-STS payload and forwards it
// as the Bearer field so the existing backend placeholder verifier accepts
// the request. SigV4-signed STS requests are opaque byte strings; the
// placeholder verifier accepts any non-empty value.
type placeholderProvider struct {
	signedRequest string
}

// NewPlaceholderProvider wraps the signed STS request payload. The
// supervisor passes this to client.ExchangeIdentity which sends it over
// the wire; the backend's placeholder verifier checks for non-empty.
func NewPlaceholderProvider(signedRequest string) Provider {
	return &placeholderProvider{signedRequest: signedRequest}
}

// Exchange returns the signed request as the Bearer field. AgentID and
// OrgID are intentionally empty — the backend assigns them and the
// supervisor stores them from the HTTP response, not from this struct.
func (p *placeholderProvider) Exchange(_ context.Context) (Credentials, error) {
	return Credentials{
		Bearer: p.signedRequest,
		// Placeholder bearer TTL: 24 hours, matching the real bearer TTL the
		// backend issues. The supervisor will refresh before it expires.
		ExpiresAt: time.Now().Add(24 * time.Hour),
	}, nil
}
