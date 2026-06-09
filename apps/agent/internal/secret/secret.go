// Package secret holds the agent's secret-redaction wrapper type.
//
// Secrets flow through the agent's command-handling path: the
// ANTHROPIC_API_KEY in `InvokeClaudeCodeCommand.invocation.exec.env`,
// the github-installation token in `ProvisionWorkspace.auth.token`, future
// per-org OAuth tokens. A wrong `log.Printf("%v", cmd)` or `json.Marshal`
// would leak those into stderr / structured logs / audit events.
//
// `Secret` wraps a string with redacted-by-default formatting:
//
//   - `String()` → `"[REDACTED]"`. Hides the value behind every default
//     printf / `fmt.Sprintf("%v", s)` / `log.Printf("%s", s)` path.
//   - `GoString()` → `"[REDACTED]"`. Hides from `%#v` too.
//   - `MarshalJSON()` → `"[REDACTED]"`. Safe to `json.Marshal` a struct
//     containing a Secret; the wire shape stays sanitised.
//   - `Value()` → the actual string. Explicit unwrap so every leak
//     site is greppable: `grep -rn '\.Value()' apps/agent`.
//
// Use Secret for any field that's a credential, even if the call site
// doesn't currently log it — defense-in-depth catches the future log
// line someone adds during incident response.
package secret

import "fmt"

// redactedPlaceholder is the string every accidental serialization
// surfaces. Picked so grepping logs for it surfaces redaction sites at
// once.
const redactedPlaceholder = "[REDACTED]"

// Secret holds a credential the agent must not log or serialize.
// Zero-value Secret (`Secret{}`) is valid — `Value()` returns empty
// string, `String()`/JSON return `"[REDACTED]"`. That keeps unset
// fields safe by construction.
//
// Secret is a struct, not a string-typedef, so `fmt.Sprintf("%s",
// s)` invokes the `String()` method via the Stringer interface
// instead of dumping the underlying bytes.
type Secret struct {
	v string
}

// New wraps `s` as a Secret. The empty string is a valid Secret value
// (caller can use it as "no credential yet" sentinel).
func New(s string) Secret {
	return Secret{v: s}
}

// Value returns the underlying credential string. This is the ONE
// path where the secret leaves redacted form. Every call site is
// grep-bait: `grep -rn '\.Value()' apps/agent` shows where the secret
// is consumed (subprocess env, HTTP headers, etc.).
func (s Secret) Value() string {
	return s.v
}

// String returns the placeholder so `fmt.Sprintf("%v", s)` / `log.Print`
// / structlog field formatting all surface `"[REDACTED]"`.
func (s Secret) String() string {
	return redactedPlaceholder
}

// GoString returns the placeholder so `%#v` formatting (used in some
// debug-print sites) doesn't leak either.
func (s Secret) GoString() string {
	return redactedPlaceholder
}

// MarshalJSON ensures `json.Marshal` of a struct containing a Secret
// emits `"[REDACTED]"` instead of the underlying value. AgentEvent
// outputs containing a Secret can be safely JSON-encoded and forwarded
// upstream.
func (s Secret) MarshalJSON() ([]byte, error) {
	return []byte(`"` + redactedPlaceholder + `"`), nil
}

// IsZero reports whether the Secret carries no value. Useful for
// "missing credential" checks without invoking `Value()` (which
// would show up in greps of credential-consuming sites).
func (s Secret) IsZero() bool {
	return s.v == ""
}

// Format honours `%s`, `%v`, `%q`, `%#v` — all redacted. Defensive
// against verbs `fmt` would otherwise route around `String()`.
func (s Secret) Format(f fmt.State, verb rune) {
	switch verb {
	case 'q':
		_, _ = fmt.Fprintf(f, "%q", redactedPlaceholder)
	default:
		_, _ = f.Write([]byte(redactedPlaceholder))
	}
}
