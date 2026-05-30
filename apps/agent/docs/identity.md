# internal/identity

> Seam that proves this agent pod's identity to the control plane.

## Scope

- **Owns:** `Provider` interface, `Credentials` struct, `placeholderProvider` implementation.
- **Does not own:** the HTTP round-trip to `/identity/exchange` (owned by `internal/protocol.Client`) or the retry/backoff loop around it (owned by `internal/supervisor`).
- **Receives:** a signed-request payload from the calling environment (env var `YAAOS_SIGNED_STS_REQUEST` in production).
- **Emits:** `Credentials` — `Bearer`, `ExpiresAt`, `AgentID`, `OrgID`.
- **Who it hands to:** `internal/supervisor` consumes `Provider.Exchange` in its startup loop and bearer-refresh loop.

## Why / invariants

- **`AgentID` and `OrgID` are empty from `placeholderProvider`** — the backend assigns them on the first exchange; the supervisor reads them from the HTTP response (`IdentityExchangeResponse`), not from this struct.
- **Fatal mismatch on renewal** — the supervisor pins `AgentID` and `OrgID` on first exchange. If `Exchange` is called again (bearer renewal) and the backend returns different values, the supervisor logs an error and calls `os.Exit(1)`. This is the identity-integrity invariant: an agent pod belongs to exactly one org and has one stable `agent_id` for its lifetime.
- **`placeholderProvider` carries the signed-STS payload as the bearer field** — the backend's placeholder verifier checks for any non-empty `signed_request`; the real STS replay replaces this without changing the supervisor.
- **`Provider` is the extension point** — a SigV4-backed implementation drops in at the `New` call site in `cmd/agent/main.go` with zero supervisor change.

## Gotchas

- `NewPlaceholderProvider` is the only constructor today. Production substitutes the real SigV4 impl at the same call site.
- `Credentials.Bearer` from `placeholderProvider` is the raw signed-request string, not a real bearer; the backend's placeholder verifier accepts it and issues a real bearer in the response.

## Vocabulary

- **Credentials** — the result of one `Exchange` call: `Bearer` (the signed-request payload forwarded to the backend), `ExpiresAt`, `AgentID`, `OrgID`.
- **Provider** — the interface the supervisor depends on; hides the STS/SigV4 implementation detail.
- **placeholderProvider** — the current implementation; forwards a pre-built signed-request string.

## Entry points

- `apps/agent/internal/identity/identity.go` — `Provider`, `Credentials`, `NewPlaceholderProvider`.
