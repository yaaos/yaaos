package secret

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// secretSentinel is the credential string our tests check is NEVER
// surfaced through default formatting paths. Picking a distinctive
// marker so a regression that leaks it shows up clearly in the failure
// message.
const secretSentinel = "ghs_super_secret_token_12345"

func TestSecret_StringMethodRedacts(t *testing.T) {
	s := New(secretSentinel)
	got := s.String()
	if got != "[REDACTED]" {
		t.Errorf("String(): want [REDACTED] got %q", got)
	}
}

func TestSecret_FmtSprintfPercentS_Redacts(t *testing.T) {
	s := New(secretSentinel)
	out := fmt.Sprintf("api_key=%s", s)
	if strings.Contains(out, secretSentinel) {
		t.Errorf("%%s leaked the secret: %q", out)
	}
}

func TestSecret_FmtSprintfPercentV_Redacts(t *testing.T) {
	s := New(secretSentinel)
	out := fmt.Sprintf("token=%v", s)
	if strings.Contains(out, secretSentinel) {
		t.Errorf("%%v leaked the secret: %q", out)
	}
}

func TestSecret_FmtSprintfPercentQ_Redacts(t *testing.T) {
	s := New(secretSentinel)
	out := fmt.Sprintf("%q", s)
	if strings.Contains(out, secretSentinel) {
		t.Errorf("%%q leaked the secret: %q", out)
	}
}

func TestSecret_FmtSprintfPercentHashV_Redacts(t *testing.T) {
	s := New(secretSentinel)
	out := fmt.Sprintf("%#v", s)
	if strings.Contains(out, secretSentinel) {
		t.Errorf("%%#v leaked the secret: %q", out)
	}
}

func TestSecret_FmtSprintfPercentPlusV_Redacts(t *testing.T) {
	// %+v on a struct field would normally dump field names + values;
	// Format() catches the verb and redacts.
	s := New(secretSentinel)
	out := fmt.Sprintf("%+v", s)
	if strings.Contains(out, secretSentinel) {
		t.Errorf("%%+v leaked the secret: %q", out)
	}
}

func TestSecret_JSONMarshalRedacts(t *testing.T) {
	type wrapped struct {
		Token Secret `json:"token"`
		Note  string `json:"note"`
	}
	w := wrapped{Token: New(secretSentinel), Note: "innocent"}
	b, err := json.Marshal(w)
	if err != nil {
		t.Fatalf("json.Marshal: %v", err)
	}
	if strings.Contains(string(b), secretSentinel) {
		t.Errorf("json.Marshal leaked the secret: %s", b)
	}
	if !strings.Contains(string(b), `"token":"[REDACTED]"`) {
		t.Errorf("expected redacted token field, got %s", b)
	}
	if !strings.Contains(string(b), `"note":"innocent"`) {
		t.Errorf("non-secret fields should pass through, got %s", b)
	}
}

func TestSecret_NestedInStruct_DoesntLeakViaStructFormat(t *testing.T) {
	// The classic leak shape: log.Printf("cmd=%+v", cmd) when cmd has a
	// credential field. Our wrapper's Format method catches every verb
	// fmt walks through.
	type cmd struct {
		ID     string
		APIKey Secret
	}
	c := cmd{ID: "c-1", APIKey: New(secretSentinel)}
	for _, verb := range []string{"%v", "%+v", "%#v", "%s"} {
		out := fmt.Sprintf("cmd="+verb, c)
		if strings.Contains(out, secretSentinel) {
			t.Errorf("verb %s leaked secret in struct: %q", verb, out)
		}
	}
}

func TestSecret_ValueReturnsActualString(t *testing.T) {
	s := New(secretSentinel)
	if s.Value() != secretSentinel {
		t.Errorf("Value() should return the actual secret; got %q", s.Value())
	}
}

func TestSecret_ZeroValueSafe(t *testing.T) {
	var zero Secret
	if zero.Value() != "" {
		t.Errorf("zero Secret.Value(): want empty, got %q", zero.Value())
	}
	if zero.String() != "[REDACTED]" {
		t.Errorf("zero Secret.String(): want [REDACTED], got %q", zero.String())
	}
	if !zero.IsZero() {
		t.Errorf("zero Secret.IsZero(): want true")
	}
}

func TestSecret_IsZero(t *testing.T) {
	if !New("").IsZero() {
		t.Errorf("Secret{empty}.IsZero() should be true")
	}
	if New("x").IsZero() {
		t.Errorf("Secret{non-empty}.IsZero() should be false")
	}
}

// Sentry against a common regression: someone changes Format() to drop
// a verb. The brute-force version of the previous tests — every printf
// verb fmt knows about gets exercised.
func TestSecret_AllVerbsRedacted(t *testing.T) {
	s := New(secretSentinel)
	for _, verb := range []string{"%s", "%v", "%+v", "%#v", "%q", "%x", "%X"} {
		out := fmt.Sprintf(verb, s)
		if strings.Contains(out, secretSentinel) {
			t.Errorf("verb %q leaked secret: %q", verb, out)
		}
	}
}
