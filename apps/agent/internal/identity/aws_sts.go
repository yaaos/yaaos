package identity

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	awssigner "github.com/aws/aws-sdk-go-v2/aws/signer/v4"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials/ec2rolecreds"
)

const (
	// stsGlobalEndpoint is the default STS endpoint. Used when
	// YAAOS_STS_ENDPOINT_URL is unset.
	stsGlobalEndpoint = "https://sts.amazonaws.com/"

	// stsEndpointEnvVar overrides the STS endpoint URL the agent signs against
	// and embeds in the signed envelope. Non-prod only — set to the mock-aws
	// URL in dev/test compose so the backend replays against the same mock.
	// SigV4 binds the host into the signature, so signing target and replay
	// target must be the same URL.
	stsEndpointEnvVar = "YAAOS_STS_ENDPOINT_URL"

	// stsAPIBody is the exact request body required by GetCallerIdentity.
	// The sts_verifier on the backend validates this value verbatim.
	stsAPIBody = "Action=GetCallerIdentity&Version=2011-06-15"

	// audienceHeader is embedded inside the signed envelope to bind the claim
	// to the backend's canonical hostname. The backend checks this header.
	audienceHeader = "X-Yaaos-Audience"
)

// resolveSTSEndpoint returns the URL the agent signs GetCallerIdentity against.
// Defaults to the global STS endpoint; overridden by YAAOS_STS_ENDPOINT_URL
// (dev/test only, points at mock-aws).
func resolveSTSEndpoint() string {
	if v := os.Getenv(stsEndpointEnvVar); v != "" {
		return v
	}
	return stsGlobalEndpoint
}

// signedEnvelope is the JSON shape expected by sts_verifier.parse_signed_request.
type signedEnvelope struct {
	URL     string            `json:"url"`
	Headers map[string]string `json:"headers"`
	Body    string            `json:"body"`
}

// awsSTSProvider signs a GetCallerIdentity request using the instance's IAM
// credentials read from IMDS v2. The signed request is never sent by the
// agent — the backend replays it against AWS STS during identity exchange.
type awsSTSProvider struct{}

func newAWSSTSProvider() Provider {
	return &awsSTSProvider{}
}

// Kind returns "aws-sts".
func (p *awsSTSProvider) Kind() string { return "aws-sts" }

// SignClaim reads IMDS credentials, builds a GetCallerIdentity HTTP request,
// sigv4-signs it (never sent), embeds an X-Yaaos-Audience header, and returns
// the JSON envelope the backend's sts_verifier expects.
//
// The X-Yaaos-Audience header is included in the signed payload so the backend
// can validate the claim was produced for it specifically.
func (p *awsSTSProvider) SignClaim(ctx context.Context, audience string) (json.RawMessage, error) {
	// Load AWS config using EC2 instance role credentials from IMDS.
	// AWS_EC2_METADATA_SERVICE_ENDPOINT is picked up automatically by the SDK
	// when set in the environment — the dev compose sets it to mock-aws.
	cfg, err := config.LoadDefaultConfig(ctx,
		config.WithCredentialsProvider(ec2rolecreds.New()),
	)
	if err != nil {
		return nil, fmt.Errorf("identity: load aws config: %w", err)
	}

	// Resolve credentials eagerly so the signer doesn't need to.
	creds, err := cfg.Credentials.Retrieve(ctx)
	if err != nil {
		return nil, fmt.Errorf("identity: retrieve imds credentials: %w", err)
	}

	// Build the GetCallerIdentity HTTP request. Body must be exactly stsAPIBody.
	body := stsAPIBody
	endpoint := resolveSTSEndpoint()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewBufferString(body))
	if err != nil {
		return nil, fmt.Errorf("identity: build sts request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	// Embed the audience header before signing so it's covered by the signature.
	// Always set — the backend requires a non-empty audience when YAAOS_PUBLIC_HOSTNAME
	// is configured, and callers must not pass an empty audience in production.
	req.Header.Set(audienceHeader, audience)

	// SigV4 sign the request. Region "us-east-1" for the global endpoint.
	signer := awssigner.NewSigner()
	h := sha256.Sum256([]byte(body))
	payloadHash := fmt.Sprintf("%x", h)
	if err := signer.SignHTTP(ctx, creds, req, payloadHash, "sts", "us-east-1", time.Now()); err != nil {
		return nil, fmt.Errorf("identity: sigv4 sign: %w", err)
	}

	// Collect all headers (SigV4 adds Authorization, X-Amz-Date, etc.).
	headers := make(map[string]string, len(req.Header))
	for k, vs := range req.Header {
		if len(vs) > 0 {
			headers[strings.ToLower(k)] = vs[0]
		}
	}

	envelope := signedEnvelope{
		URL:     endpoint,
		Headers: headers,
		Body:    body,
	}
	out, err := json.Marshal(envelope)
	if err != nil {
		return nil, fmt.Errorf("identity: marshal envelope: %w", err)
	}
	return json.RawMessage(out), nil
}
