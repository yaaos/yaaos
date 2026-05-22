# yaaos security posture (no-private-routing model)

Hybrid SaaS architecture. Control plane hosted by yaaos.ai handles orchestration, UI, and persisted metadata. A customer-deployed workspace agent inside the customer's environment (typically a customer-controlled VPC or cluster) handles all code-touching work. Source code never enters yaaos infrastructure. Only structured findings, comment threads, and operational metadata cross the boundary. The architecture is designed so that no single credential leak — on either side — gives an attacker the ability to read customer source code or impersonate a customer in the control plane.

## Network

Public-internet HTTPS in both directions: workspace-to-control-plane (outbound from the customer's environment) and webhook ingress from GitHub. No inbound port opened on the customer side. The security guarantee does not depend on network isolation; it depends on TLS for transport confidentiality plus federated identity proofs at the application layer (AWS-signed or OIDC JWT), the same model used by every major B2B SaaS.

Optional per-workspace source-IP allowlist enforced at the Cloudflare edge before any compute spins up. Useful as defense in depth for customers running the agent in static-IP deployments; explicitly opt-in because ephemeral-IP runtimes (GH Actions, dev laptops) can't usefully populate it.

## Authentication

Anchored in runtime-issued federated identity — the WorkspaceAgent never holds a long-lived shared secret. Two accepted issuers:

- **AWS STS (sigv4)** — agent on AWS ECS/EC2 signs an STS `GetCallerIdentity` call with its task IAM role; control plane replays the signed call against AWS to confirm the role ARN matches the customer's registered identity. (Vault AWS auth pattern.)
- **Generic OIDC** — agent on GCP, Azure, EKS, Kubernetes, Fly.io, or GitHub Actions fetches a short-lived OIDC token from its runtime's metadata service; control plane verifies the JWT against the issuer's JWKS and matches `iss` + `aud` + `sub` claim pattern to the customer's registered federation config.

Both flows yield the same property: identity is verified against the upstream platform on every bootstrap, no shared secret is in motion or at rest, and the customer revokes by changing trust at the issuer (no yaaos cooperation needed). OIDC is a strict generalization of sigv4 — same security model, broader runtime coverage. The AWS-ECS-without-EKS case has no native OIDC endpoint, so sigv4 remains the AWS path; OIDC covers everything else.

**Bare VM / dev laptop fallback.** No native IdP — agent exchanges a one-time bootstrap token (single-use, ≤15min TTL, workspace-scoped) for a locally-generated ed25519 keypair on first run. The private key never leaves the host; subsequent calls sign short-lived JWTs verifiable against the stored public key. Documented POC limitation; not recommended for production deployments.

Yaaos's marketplace GitHub App provides scoped, short-lived installation tokens for repository access. The App's private key is the only long-lived secret yaaos holds; it sits in KMS-managed storage with audit logging on every retrieval.

## Sessions

All yaaos sessions — browser and workspace agent — are **opaque, server-side, revocable** tokens. No yaaos-issued JWTs. The token is 32 bytes of randomness, stored hashed in Postgres alongside `user_id` or `workspace_id`, `current_org_id`, `created_at`, `last_seen_at`, `expires_at`, `ip`, `user_agent`. Lookup is one indexed query per request. Revocation is row deletion (logout, role change, suspicious activity, admin force-logout).

For browser sessions the cookie is `HttpOnly`, `Secure`, `SameSite=Lax`. Lax preserves the shared-link UX (clicking a yaaos URL from Slack/email opens logged in) while still blocking cross-site state-changing requests; explicit anti-CSRF tokens (double-submit) protect all state-changing endpoints as belt-and-suspenders.

For workspace agents the initial federated identity proof (AWS-signed blob or OIDC JWT) is exchanged for an opaque session token; the workspace stores it in memory and sends it on every subsequent call. Expiry forces re-bootstrap against the customer's IdP — cheap, and ensures the underlying trust at the issuer is still valid.

JWTs are reserved for tokens **signed by external systems** that yaaos verifies — AWS STS / OIDC identity proofs at the workspace bootstrap, GitHub webhook HMAC signatures. Yaaos itself never issues JWTs, eliminating signing-key rotation, `alg: none` attacks, and the inability-to-revoke-before-expiry problem.

## Workspace lifecycle and scaling

Workspaces are long-running workers that the customer deploys and sizes — AWS ECS is the documented default, but the same shape works on any container runtime (GKE, AKS, Fly Machines, plain VMs). The control plane never calls customer-side cloud APIs in the default model — it only adds work to a per-org queue. Agents poll over outbound HTTPS, pick up jobs, and post results back.

Per-review isolation happens inside the agent: each job runs in a fresh subdirectory that is created at job start and deleted at job end. The agent process is long-lived; the per-review sandbox is not. Source code, cloned repos, and intermediate artifacts never persist across jobs.

Scaling on AWS is driven by the customer's own ECS Service Auto Scaling, using a target-tracking policy on a CloudWatch metric. The metric value comes from the workspace agents themselves: on each poll, the control plane returns current queue depth, and the agent emits it via `cloudwatch:PutMetricData` under its existing IAM role. No new credential, no separate identity — the agent's role gets one additional permission scoped to a single CloudWatch namespace. Non-AWS runtimes follow the same shape using each platform's native autoscaling (HPA on Kubernetes, Cloud Run autoscaling on GCP, Fly Machines autoscaling); the only platform-specific glue is the metrics-emit verb.

This pattern keeps yaaos out of the customer's cloud account entirely. Yaaos publishes a metrics endpoint; the customer's orchestrator does the rest as a standard managed service. A cross-account `ecs:RunTask` (or per-platform equivalent) model exists as an enterprise option if a customer ever needs per-job spin-up with scale-to-zero, but it is not the default because it requires yaaos to assume a role inside the customer's cloud account.

- **No control-plane-initiated workspace spawning.** Workspaces are customer-deployed long-running workers; the control plane queues jobs, never launches infrastructure.
- **Per-review isolation via ephemeral subdirectories.** Each job's working tree is created and destroyed inside the agent process; nothing persists across jobs.
- **Scaling uses the customer's native autoscaling.** AWS ECS target-tracking is the documented default; HPA / Cloud Run / Fly Machines autoscaling is the equivalent on other platforms.
- **Queue-depth metric is emitted by agents under their existing runtime identity**; on AWS the only added permission is `cloudwatch:PutMetricData` scoped to the `yaaos/workspace` namespace.
- **PutMetricData call volume is two orders of magnitude below AWS rate limits** at expected agent counts, and well within the CloudWatch free tier on cost.
- **Cross-account `ecs:RunTask` (or per-platform equivalent) is an enterprise option, not the default.** The default model gives yaaos zero permissions in the customer's cloud account.

## LLM access — BYOK required

Bring-your-own-key is mandatory. The customer registers their own Anthropic/OpenAI account; the workspace agent uses that key directly when making model calls. All prompt content — including diff context the agent fetches via tool calls — flows from the workspace to the LLM provider over the customer's own commercial relationship. Yaaos never holds, proxies, or sees the customer's LLM credentials, and yaaos's infrastructure never sees prompt or response bodies. The only LLM-related data persisted in yaaos is the structured finding output the agent returns.

Code excerpts inside finding bodies are the explicit exception to "source stays in VPC" and are surfaced through the published finding schema so customers know exactly what crosses the boundary.

## Multi-tenancy and data at rest

Shared Postgres with strict `org_id` discipline at the repository layer, CI-enforced tenant-isolation tests, and an append-only audit log per organization. Data at rest is encrypted via managed database encryption with KMS-wrapped keys. Sensitive runtime values (the GitHub App private key, any customer-supplied credentials) use a separate KMS key from general application data, with access logged at the KMS layer.

## Distinguishing properties

- **No source code in yaaos.** Even a full compromise of the yaaos backend cannot exfiltrate customer source; the data model contains none.
- **BYOK is mandatory.** All AI processing runs through the customer's own LLM provider account; yaaos never holds, proxies, or sees those keys or the prompt content.
- **No long-lived per-customer secret in yaaos.** Per-org config is a federation reference (IAM role ARN, or OIDC issuer + audience + subject pattern) plus a GitHub installation ID — all non-secret values.
- **Public workspace image is safe to leak.** Identity comes from the runtime's IdP at boot, not from anything baked into the image.
- **Customer revokes in two steps, no yaaos cooperation needed.** Revoke trust at the federation issuer (detach IAM trust policy / delete OIDC service account / revoke runtime identity) and uninstall the GitHub App.
