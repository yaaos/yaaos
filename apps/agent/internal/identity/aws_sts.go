package identity

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	awssigner "github.com/aws/aws-sdk-go-v2/aws/signer/v4"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials/ec2rolecreds"
)

const (
	// stsGlobalEndpoint is the default STS endpoint. The backend uses the
	// global endpoint for mock-aws compatibility in dev/test.
	stsGlobalEndpoint = "https://sts.amazonaws.com/"

	// stsAPIBody is the exact request body required by GetCallerIdentity.
	// The sts_verifier on the backend validates this value verbatim.
	stsAPIBody = "Action=GetCallerIdentity&Version=2011-06-15"

	// audienceHeader is embedded inside the signed envelope to bind the claim
	// to the backend's canonical hostname. The backend checks this header.
	audienceHeader = "X-Yaaos-Audience"
)

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
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, stsGlobalEndpoint, bytes.NewBufferString(body))
	if err != nil {
		return nil, fmt.Errorf("identity: build sts request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	// Embed the audience header before signing so it's covered by the signature.
	if audience != "" {
		req.Header.Set(audienceHeader, audience)
	}

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
		URL:     stsGlobalEndpoint,
		Headers: headers,
		Body:    body,
	}
	out, err := json.Marshal(envelope)
	if err != nil {
		return nil, fmt.Errorf("identity: marshal envelope: %w", err)
	}
	return json.RawMessage(out), nil
}
