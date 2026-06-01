# internal/identity

> Seam that signs the agent pod's identity claim for the control plane.

## Scope

- **Owns:** `Provider` interface, `Credentials` struct, `awsSTSProvider` implementation, `NewProvider` factory.
- **Does not own:** the HTTP exchange to `/api/v1/agent/identity` (owned by `internal/supervisor`), retry/backoff (owned by `internal/supervisor`), credential verification (owned by the backend's `sts_verifier`).
- **Receives:** nothing at construction — credentials are read lazily from IMDS at `SignClaim` time.
- **Emits:** `json.RawMessage` — the JSON-encoded sigv4-signed envelope passed as the `payload` field in `IdentityExchangeRequest`.
- **Who it hands to:** `internal/supervisor` calls `provider.SignClaim(ctx, audience)` then builds and POSTs the full request.

## Why / invariants

- **Supervisor owns the HTTP exchange.** `Provider` only signs; it never contacts the backend. This keeps retry/backoff logic, `AgentMetadata` collection, and bearer stamping in one place (supervisor).
- **`NewProvider` dispatches on `YAAOS_IDENTITY_PROVIDER`** (default `aws-sts`). Only `aws-sts` is defined; any other value panics at startup so a misconfigured pod fails fast instead of silently falling back.
- **`awsSTSProvider` reads IMDS v2 at sign time.** Credentials are not cached by the provider — `aws.NewCredentialsCache` inside `config.LoadDefaultConfig` handles caching/refresh. The env var `AWS_EC2_METADATA_SERVICE_ENDPOINT` redirects IMDS to mock-aws in dev/test.
- **Audience binding.** `X-Yaaos-Audience` is embedded inside the signed envelope before signing, so it's covered by the SigV4 signature and cannot be stripped by an attacker without invalidating the signature.
- **`Credentials` is stamped by the supervisor from the backend response**, not by the provider. `Provider` never fills `Bearer`, `AgentID`, `OrgID`, or `InstanceID`.
- **Identity-integrity invariant:** supervisor pins `AgentID`, `OrgID`, and `InstanceID` from the first exchange; any mismatch on renewal triggers a fatal exit.

## Gotchas

- `awsSTSProvider.SignClaim` contacts IMDS on every call. The call context should have a reasonable deadline — the supervisor passes the same ctx it uses for the exchange loop.
- In tests, the supervisor is constructed with a stub provider that implements `Kind() + SignClaim()` directly; `NewProvider()` is not called from tests.

## Vocabulary

- **Provider** — the interface the supervisor depends on; hides the IMDS/SigV4 detail.
- **awsSTSProvider** — the production implementation; reads IMDS v2 creds, builds and sigv4-signs a `GetCallerIdentity` request, returns the JSON envelope.
- **Credentials** — stamped by the supervisor from the backend's exchange response: `Bearer`, `ExpiresAt`, `AgentID`, `OrgID`, `InstanceID`. Never populated by the provider.
- **Audience** — the backend's canonical hostname embedded as `X-Yaaos-Audience` inside the signed envelope.

## Entry points

- `apps/agent/internal/identity/identity.go` — `Provider` interface, `Credentials` struct, `NewProvider` factory.
- `apps/agent/internal/identity/aws_sts.go` — `awsSTSProvider.Kind()`, `awsSTSProvider.SignClaim()`.
