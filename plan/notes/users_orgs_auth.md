# Users, orgs, auth, permissions

Reference model for identity, tenancy, and access in yaaos. Drives the data model and the login flow.

## 1. Users

- **Primary key is UUID**, never email. Email changes; identity doesn't.
- **Multiple verified emails per user.** One marked `primary` (used for notifications); sign-in works against any verified email.
- **Display name + handle** stored separately. Display name is freeform; handle is unique-per-instance and used in @mentions.
- **Linked GitHub username, etc.** are purely additive (`oauth_identities` rows, see §4).
- **Soft-delete only.** Removing a user marks the row `deactivated` but keeps it (audit-log entries reference user_id forever). Emails on deactivated rows become available for re-use.
- **Users belong to ≥1 org.** A user with zero org memberships still exists as a row (allows re-invite); no UI shown to that user.

## 2. Orgs

- **Primary key is UUID.** Plus a `slug` for URLs (`yaaos.io/orgs/<slug>/...`). Slug is unique and immutable after creation.
- **All non-user data lives under an org.** Workspaces, BYOK keys, GitHub App installations, findings, threads, audit log — everything is `org_id`-scoped.
- **Soft-delete only.** Archived orgs retain data for the audit-retention period and can be restored.
- **No personal / single-user orgs in POC.** Even one human gets a real org. Keeps the model uniform.

## 3. Permissions

Three roles, assigned per-user per-org:

- **Owner** — full control: member management, role assignment, billing (when there's billing), org settings, BYOK keys, IAM role registration, GitHub App linking, org deletion. At least one Owner per org always.
- **Admin** — everything Owner does *except* delete the org or remove other Owners.
- **Member** — read findings, post replies via PR comments, trigger reviews, manage acknowledgments on findings they raised. No settings access, no member management.

Rules:

- A user's role in org A does not carry over to org B. Roles are per-membership.
- Adding a fourth role (Viewer, etc.) is deferred until a customer asks. Three roles is enough for POC.
- Permission checks are per-action, per-org, evaluated at every state-changing endpoint and every read endpoint that surfaces org-scoped data.

### Service accounts / API tokens

- Owned by an org, not by a user.
- Assigned a role from the same enum.
- Used by automation (CI scripts triggering reviews, etc.).
- Token format: random secret with visible prefix (`yaaos_pat_…`) so secret scanners catch accidental commits. Stored hashed in DB; shown once at creation.
- Revocable; revocation is row deletion.

## 4. Auth — provider-agnostic, GitHub first

Auth uses a generic OAuth/OIDC identity-provider framework. GitHub is the first provider shipped; Google, Microsoft, etc. are additive — each is a row in a `providers` table plus a callback handler.

- **Linked identities** stored in `oauth_identities` (`user_id`, `provider`, `external_subject`, `verified_at`). A user can have multiple rows.
- **Email matching is on verified email only.** Providers' `email_verified` claim must be true.
- **Account-linking rule**: if a first-time login at provider B returns an email already in the system, **require the user to authenticate via their existing provider A first** to confirm the link. Auto-link without confirmation lets an attacker who controls provider B for the email take over the existing account.
- **Passwords**: yaaos owns no passwords. Identity is delegated to OAuth/OIDC providers.

### 2FA

- Required globally for all human users.
- When signing in via GitHub (or any IdP that returns `acr` / `amr` indicating MFA), yaaos accepts the provider's 2FA as satisfying its own requirement.
- If a future provider doesn't enforce 2FA, yaaos requires a TOTP step before issuing a session.

### SSO (SAML 2.0)

- Per-org Owner-configurable.
- One IdP per org for POC. (Multi-IdP and cross-org SSO are deferred.)
- **SSO enforcement is org-level**: when on, members must satisfy SSO after each yaaos login.
- **JIT provisioning** opt-in per org: anyone authenticating via the org's IdP whose verified email-domain matches becomes a `Member` automatically. Off by default — invitations are the access gate.
- **Break-glass Owner**: one Owner per org can be marked SSO-exempt at SSO setup time. Used when the IdP is down. Every use is audit-logged. Without this, an IdP outage locks the whole org out.
- Workspace agents are unaffected by SSO — they auth via AWS IAM, which is a separate, machine-only path.

### Invitations

- Owner/Admin enters email → invitation email with signed token link → recipient signs in via any configured provider → membership created with chosen role.
- Tokens expire in 7 days.
- Invitations are the access gate: a user with a yaaos account but no membership in org X has zero visibility into org X.

### Decoupling from GitHub repo permissions

- Whether a user sees findings on a given repo is determined by their **org membership and role**, not by their GitHub permissions on the underlying repo.
- The org's GitHub App installation is the access gate from yaaos to the repo. The user's GitHub permissions are not checked per-request.
- Trade-off: simpler, no per-request GitHub permission lookups. Invitation discipline is the security control.

## 5. Sessions

All sessions — browser and workspace — are **opaque, server-side, revocable** tokens. No yaaos-issued JWTs. See `security-posture.md` § Sessions for the full mechanics. Summary:

- 32 random bytes, hashed in DB, with `user_id` (or `workspace_id`), `current_org_id`, `created_at`, `last_seen_at`, `expires_at`, `ip`, `user_agent`.
- Browser: `HttpOnly`, `Secure`, `SameSite=Lax` cookie + double-submit anti-CSRF tokens on state-changing endpoints.
- Workspace: opaque token issued after the AWS-signed identity bootstrap; refreshed by re-bootstrapping.
- Revoke = delete the row. Logout, role change, suspicious activity, admin force-logout — all the same operation.

## 6. Org switching

- URL-scoped (`/orgs/<slug>/...`). Session is org-agnostic; `current_org_id` on the session row is the user's last-used org for UI defaults but is not authoritative.
- Every request validates that the session user has membership in the org named in the URL with sufficient role.

## 7. Audit log

Every auth/membership event written append-only per org:

- Login, logout (including reason: explicit logout / expiry / forced).
- Provider link / unlink.
- SSO config change, SSO-exempt flag toggle, SSO-exempt-Owner login.
- Member invited / accepted / removed / role-changed.
- API token created / used / revoked.
- Role-elevated action by Owners/Admins.

Retained per org's contractual period.

## 8. POC explicit cuts

- **SCIM auto-deprovisioning** — manual member removal only.
- **Custom roles / fine-grained permissions** — the three-role enum is fixed.
- **Email magic-link as primary auth** — OAuth-only at launch (one less attack surface).
- **Multiple SSO providers per org** — one IdP per org.
- **Cross-org SSO linkage** (one IdP serving multiple yaaos orgs) — one-to-one.
- **Per-finding visibility based on GitHub repo permissions** — see §4 decoupling note.
- **Personal orgs / personal access mode** — every user is in at least one real org.

## 9. Decisions made

- **User ↔ GitHub repo access is decoupled.** Invitation + role is the access gate; yaaos does not check the user's per-repo GitHub permission on each request.
- **User rows are kept indefinitely** after org removal. Storage is cheap; reactivation is common; audit-log integrity is the win.
- **OAuth is the only login mechanism**; passwords never live in yaaos.
- **JWTs are not used for yaaos-issued sessions**; opaque server-side tokens everywhere.
- **`SameSite=Lax`** on session cookies — preserves shared-link UX; double-submit CSRF tokens cover the state-change vector.
- **SAML 2.0** is the POC SSO protocol; OIDC SSO can be added later via the same provider framework.
