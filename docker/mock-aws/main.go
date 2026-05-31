// mock-aws serves two AWS API surfaces used by the dev/test agent identity
// exchange flow:
//
//   - IMDS v2 (Instance Metadata Service) — vends ephemeral IAM credentials
//     so the agent's awsSTSProvider can obtain SigV4 signing keys without a
//     real EC2 instance.
//
//   - STS GetCallerIdentity — validates the SigV4 envelope is well-formed and
//     returns a fixed assumed-role ARN whose role-session-name is the configured
//     YAAOS_DEV_SEED_INSTANCE_ID (default: "dev-task-00000000").
//
// Both surfaces are intentionally minimal: enough to exercise the identity
// exchange contract end-to-end in dev/test, not a full AWS API emulator.
//
// Environment variables:
//
//	YAAOS_DEV_SEED_ARN      — the IAM role ARN registered in the org row.
//	                          mock-aws derives the assumed-role ARN from it.
//	                          Default: "arn:aws:iam::000000000000:role/yaaos-dev"
//	YAAOS_DEV_SEED_INSTANCE_ID — role-session-name embedded in the returned ARN.
//	                              Default: "dev-task-00000000"
//	PORT                    — HTTP listen port. Default: 4566.
package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/xml"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// seedARN is the IAM role ARN the org was seeded with.
var seedARN = envOr("YAAOS_DEV_SEED_ARN", "arn:aws:iam::000000000000:role/yaaos-dev")

// instanceID is the role-session-name embedded in the STS assumed-role ARN.
var instanceID = envOr("YAAOS_DEV_SEED_INSTANCE_ID", "dev-task-00000000")

// derivedAssumedRoleARN builds the assumed-role form from the seed IAM role ARN.
// Input:  arn:aws:iam::ACCOUNT:role/ROLE
// Output: arn:aws:sts::ACCOUNT:assumed-role/ROLE/<instanceID>
func derivedAssumedRoleARN() string {
	// Parse account + role from the seed IAM ARN.
	// Format: arn:aws:iam::<ACCOUNT>:role/<ROLE>
	parts := strings.SplitN(seedARN, ":", 6)
	if len(parts) < 6 {
		return fmt.Sprintf("arn:aws:sts::000000000000:assumed-role/yaaos-dev/%s", instanceID)
	}
	account := parts[4]
	roleField := parts[5] // "role/ROLE_NAME"
	roleName := strings.TrimPrefix(roleField, "role/")
	return fmt.Sprintf("arn:aws:sts::%s:assumed-role/%s/%s", account, roleName, instanceID)
}

// ── IMDS v2 ──────────────────────────────────────────────────────────────

// handleIMDSToken handles PUT /latest/api/token (IMDSv2 token request).
// The AWS SDK sends this before requesting credentials. We return a fixed
// opaque token; any value is accepted on subsequent GET calls.
func handleIMDSToken(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPut {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	w.Header().Set("Content-Type", "text/plain")
	w.Header().Set("X-Aws-Ec2-Metadata-Token-Ttl-Seconds", "21600")
	fmt.Fprint(w, "mock-imds-token-v2")
}

// handleIMDSCredentials handles GET /latest/meta-data/iam/security-credentials/<profile>.
// Returns ephemeral fake credentials that the AWS SigV4 signer accepts.
func handleIMDSCredentials(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	// The SDK first calls the profile-listing endpoint (no profile in path),
	// then the specific-profile endpoint.
	path := r.URL.Path
	if path == "/latest/meta-data/iam/security-credentials/" || path == "/latest/meta-data/iam/security-credentials" {
		w.Header().Set("Content-Type", "text/plain")
		fmt.Fprint(w, "yaaos-dev-profile")
		return
	}
	// Credential response for any profile name.
	now := time.Now().UTC()
	expiry := now.Add(1 * time.Hour)
	// Build a fake but structurally valid HMAC-keyed access key and secret so
	// the SigV4 signer can produce a well-formed (but fake) Authorization header.
	// mock-aws accepts any SigV4-shaped request regardless of signature validity.
	fakeSecret := hmacHex("mock-secret-key", now.Format("20060102"))
	creds := fmt.Sprintf(`{
  "Code": "Success",
  "LastUpdated": %q,
  "Type": "AWS-HMAC",
  "AccessKeyId": "ASIAMOCKAWSACCESSKEYID",
  "SecretAccessKey": %q,
  "Token": "mock-session-token-for-dev-only",
  "Expiration": %q
}`, now.Format(time.RFC3339), fakeSecret, expiry.Format(time.RFC3339))
	w.Header().Set("Content-Type", "application/json")
	fmt.Fprint(w, creds)
}

func hmacHex(key, data string) string {
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte(data))
	return hex.EncodeToString(mac.Sum(nil))
}

// ── STS GetCallerIdentity ─────────────────────────────────────────────────

// stsResponse is the XML body for a successful GetCallerIdentity response.
type stsResponse struct {
	XMLName xml.Name           `xml:"GetCallerIdentityResponse"`
	NS      string             `xml:"xmlns,attr"`
	Result  callerIdentity     `xml:"GetCallerIdentityResult"`
	Metadata responseMetadata  `xml:"ResponseMetadata"`
}

type callerIdentity struct {
	Arn     string `xml:"Arn"`
	UserID  string `xml:"UserId"`
	Account string `xml:"Account"`
}

type responseMetadata struct {
	RequestID string `xml:"RequestId"`
}

// handleSTS handles POST / (the STS endpoint path used by GetCallerIdentity).
// It validates the request is superficially SigV4-shaped (has an Authorization
// header), then returns the configured assumed-role ARN.
func handleSTS(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// Minimal validation: the Authorization header must be present and look
	// like a SigV4 header. We don't verify the signature — this is dev/test only.
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, "AWS4-HMAC-SHA256") {
		w.Header().Set("Content-Type", "text/xml")
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprint(w, `<ErrorResponse><Error><Code>InvalidAction</Code><Message>missing SigV4 Authorization</Message></Error></ErrorResponse>`)
		return
	}

	arn := derivedAssumedRoleARN()
	account := extractAccount(seedARN)

	resp := stsResponse{
		NS: "https://sts.amazonaws.com/doc/2011-06-15/",
		Result: callerIdentity{
			Arn:     arn,
			UserID:  fmt.Sprintf("AROA%s:%s", strings.ToUpper(instanceID[:min(12, len(instanceID))]), instanceID),
			Account: account,
		},
		Metadata: responseMetadata{RequestID: "mock-request-id-00000000-0000-0000-0000-000000000000"},
	}

	out, err := xml.MarshalIndent(resp, "", "  ")
	if err != nil {
		http.Error(w, "marshal error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/xml")
	w.WriteHeader(http.StatusOK)
	w.Write([]byte(xml.Header))
	w.Write(out)
}

func extractAccount(arn string) string {
	// arn:aws:iam::<ACCOUNT>:role/...
	parts := strings.SplitN(arn, ":", 6)
	if len(parts) >= 5 {
		return parts[4]
	}
	return "000000000000"
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// ── main ──────────────────────────────────────────────────────────────────

func main() {
	port := envOr("PORT", "4566")
	addr := ":" + port

	mux := http.NewServeMux()

	// IMDSv2 token endpoint.
	mux.HandleFunc("/latest/api/token", handleIMDSToken)
	// IMDS credentials (profile list + specific profile).
	mux.HandleFunc("/latest/meta-data/iam/security-credentials/", handleIMDSCredentials)
	mux.HandleFunc("/latest/meta-data/iam/security-credentials", handleIMDSCredentials)
	// STS endpoint.
	mux.HandleFunc("/", handleSTS)

	log.Printf("mock-aws listening on %s (ARN=%s, instance_id=%s)", addr, derivedAssumedRoleARN(), instanceID)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("mock-aws: %v", err)
	}
}
