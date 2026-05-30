package identity

import (
	"context"
	"testing"
	"time"
)

func TestPlaceholderProvider_Exchange_ReturnsCannedCredentials(t *testing.T) {
	const cannedRequest = "test-signed-sts-request-payload"
	p := NewPlaceholderProvider(cannedRequest)

	creds, err := p.Exchange(context.Background())
	if err != nil {
		t.Fatalf("Exchange returned unexpected error: %v", err)
	}

	if creds.Bearer != cannedRequest {
		t.Errorf("Bearer: want %q, got %q", cannedRequest, creds.Bearer)
	}
	if creds.AgentID != "" {
		// placeholder provider does not know the AgentID — that's assigned
		// by the backend on exchange; the placeholder returns it empty.
		t.Errorf("AgentID: want empty from placeholder, got %q", creds.AgentID)
	}
	if creds.OrgID != "" {
		t.Errorf("OrgID: want empty from placeholder, got %q", creds.OrgID)
	}
	// ExpiresAt should be in the future.
	if !creds.ExpiresAt.After(time.Now()) {
		t.Errorf("ExpiresAt: want future, got %v", creds.ExpiresAt)
	}
}

func TestPlaceholderProvider_Exchange_SignedRequestPassthrough(t *testing.T) {
	// The placeholder's sole job is to carry the signed request as the
	// Bearer field so the existing backend verifier accepts it.
	payload := `{"url":"https://sts.amazonaws.com/","headers":{},"body":""}`
	p := NewPlaceholderProvider(payload)

	creds, err := p.Exchange(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if creds.Bearer != payload {
		t.Errorf("want Bearer == signed request payload, got %q", creds.Bearer)
	}
}
