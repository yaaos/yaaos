// Package identity defines the seam between the supervisor and the mechanism
// that proves this agent pod's identity to the control plane.
//
// The Provider interface abstracts over the signing protocol so the supervisor
// does not depend on the concrete verification method. The supervisor owns the
// HTTP exchange — Provider only signs the claim; it never contacts the backend.
//
// `NewProvider` is the factory; it selects the implementation based on the
// `YAAOS_IDENTITY_PROVIDER` env var (default: "aws-sts").
//
// Identity-integrity invariant: the backend assigns AgentID and OrgID on the
// first exchange. The supervisor pins them and verifies they are unchanged on
// every bearer renewal — a mismatch is fatal.
package identity

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
)

// providerEnvVar selects the Provider implementation. Default: "aws-sts".
const providerEnvVar = "YAAOS_IDENTITY_PROVIDER"

// kindAWSSTS is the only Provider kind defined today.
const kindAWSSTS = "aws-sts"

// Credentials holds the result of a successful identity exchange as stamped
// by the supervisor from the backend's response. The Provider never fills
// these fields — it only produces the claim payload.
//
// AgentID and OrgID are pinned by the supervisor on first exchange and must
// remain stable across renewals. Bearer and ExpiresAt rotate on each renewal.
//
// InstanceID is the backend-assigned pod identifier (role-session-name from
// the STS assumed-role ARN). Stable across pod restarts that reuse the same
// session name.
type Credentials struct {
	Bearer     string
	ExpiresAt  string // RFC3339 string as returned by the backend
	AgentID    string
	OrgID      string
	InstanceID string
}

// Provider signs the identity claim for this agent pod. The supervisor sends
// the claim to the backend in `POST /api/v1/agent/identity`; the backend
// replays it against AWS STS to verify.
//
// Provider does NOT contact the backend — it only produces a signed payload.
// The supervisor owns the HTTP transport, so retry/backoff live outside this
// interface.
type Provider interface {
	// Kind returns the claim type string sent in the wire request's `kind` field.
	// Today only "aws-sts" is defined.
	Kind() string

	// SignClaim signs a GetCallerIdentity request with the pod's IAM credentials
	// and returns the JSON-encoded envelope the backend's sts_verifier expects.
	// The envelope shape:
	//   {"url": "...", "headers": {...}, "body": "Action=GetCallerIdentity&Version=2011-06-15"}
	//
	// audience is embedded as an `X-Yaaos-Audience` header inside the signed
	// envelope so the backend can validate the claim was produced for it.
	//
	// Returns (json.RawMessage, error). The raw JSON is passed as `payload` in
	// the identity-exchange request body — the supervisor marshals the outer
	// envelope.
	SignClaim(ctx context.Context, audience string) (json.RawMessage, error)
}

// NewProvider constructs the appropriate Provider for the current environment.
// The YAAOS_IDENTITY_PROVIDER env var selects the implementation; it defaults
// to "aws-sts". An unknown value causes a panic at startup.
//
// Environment variables read by each provider:
//
//	aws-sts: AWS_EC2_METADATA_SERVICE_ENDPOINT (IMDS URL, default auto-detected)
//	         AWS_REGION (optional; the signed URL carries the region instead)
func NewProvider() Provider {
	kind := os.Getenv(providerEnvVar)
	if kind == "" {
		kind = kindAWSSTS
	}
	switch kind {
	case kindAWSSTS:
		return newAWSSTSProvider()
	default:
		panic(fmt.Sprintf("identity: unknown %s=%q (known: %q)", providerEnvVar, kind, kindAWSSTS))
	}
}
