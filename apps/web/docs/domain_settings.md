# domain/settings

> Four independent cards: GitHub App, Model API key, Workspace provider, Plugin health. No gating between them.

## Purpose

The `/settings` page. Four peer cards, each standing alone. Operators save the Anthropic key whether or not GitHub is installed, paste GitHub credentials whether or not Anthropic is configured, pick the workspace provider whether or not anything else is configured, and the plugin-health card iterates whatever plugins exist without hardcoding them.

## Public interface

- `SettingsPage` — mounted by `core/routing` at `/settings`. All subcomponents private (`GitHubAppCard`, `NoAppBody`, `AppCreatedBody`, `InstalledBody`, `ManifestForm`, `CredentialsForm`, `ApiKeyCard`, `WorkspaceSettingsCard`, `ConnectionStatusLine`, `PluginHealthCard`, `RepositoriesList`).

## Module architecture

`apps/web/src/domain/settings/index.tsx` is a single ~730-LOC file, four cards stacked.

### Card 1 — GitHub App

`<GitHubAppCard>` reads `useGithubInstallation()` and dispatches by state:

| `credentials_configured` | `installed` | Body |
|---|---|---|
| false | — | `<NoAppBody>` — manifest CTA + collapsible credentials-paste |
| true | false | `<AppCreatedBody>` — "App created · not installed" with Install link |
| true | true | `<InstalledBody>` — "Installed on @org · Xm ago" + repos + Configure-on-GitHub link |

Header badge (`data-testid="github-status"`): `no app` (danger), `app created · not installed` (soft), `installed` (success).

### `NoAppBody` — Manifest Flow primary; paste secondary

Primary CTA: `<ManifestForm>`. Operator enters their webhook URL, clicks Create GitHub App, which posts a manifest to `https://github.com/settings/apps/new`. GitHub redirects back to `/api/github/manifest-callback?code=...`; the backend exchanges the code at `POST /app-manifests/{code}/conversions` to receive App ID / slug / PEM / webhook secret, stores them encrypted, and 303s to `https://github.com/apps/{slug}/installations/new` so the operator picks an install target.

Escape hatch: a `<details>` block labelled "Already have an App? Enter it manually" wraps `<CredentialsForm>` (App ID / slug / PEM / webhook secret) → `useSetGithubCredentials` → `POST /api/github/credentials`. On success the card unmounts the form and flips to `<AppCreatedBody>`.

### `InstalledBody` — Repositories list

`<RepositoriesList>` reads `useGithubRepositories()` (`GET /api/github/repositories`, which proxies GitHub's `/installation/repositories` via the live install token). Shows full name + privacy icon. Repo access is changed only via GitHub's install settings, reached via **Configure on GitHub** (`data.installations_url`).

### Card 2 — Model API key

`<ApiKeyCard>` shows:
- Header badge (`data-testid="apikey-status"`) — `configured` (success) when `onboarding.anthropic_key_set` is true, `not set` (danger) otherwise.
- Password input + Save (`anthropic-key` / `anthropic-save`) → `useSetAnthropicKey` → `POST /api/claude_code/api_key`. Always editable, no cross-card gating.
- Inline `Saved.` confirmation (`anthropic-saved`) on success.

`onboarding.anthropic_key_set` is authoritative from the backend — true only when the key authenticates against Anthropic (or when `YAAOS_CODING_AGENT_STUB=1` short-circuits). A typo keeps the badge red.

### Card 3 — Workspace provider

`<WorkspaceSettingsCard>` reads `useOrgSettings()` (`GET /api/orgs`, slice 85) and dispatches by `workspace_provider`:

- Header badge (`data-testid="workspace-status"`): `in-process` (soft) when `in_memory`, `remote agent` (soft) when `remote_agent`, `not configured` (danger) when null.
- Provider dropdown (`workspace-provider-select`) lists `— not configured —` / `in-process` / `remote agent`.
- ARN input (`workspace-arn`) appears only when `remote agent` is picked; placeholder shows the canonical AWS ARN shape.
- Save button (`workspace-save`) disables when nothing changed or when `remote_agent` is picked without an ARN; inline danger text explains the ARN requirement. Mutates via `useUpdateOrgSettings` → `PATCH /api/orgs`; invalidates both the org-settings and connection-status caches on success.

When `remote_agent` is the saved value, `<ConnectionStatusLine>` polls `useWorkspaceConnectionStatus(true)` (`GET /api/workspaces/connection_status`) every 3s and renders one of three badges:

| `state`            | Badge          | Meaning                                                |
|--------------------|----------------|--------------------------------------------------------|
| `connected`        | success        | At least one pod heartbeated in the last 90s.          |
| `lost`             | danger         | Pods registered but none recent enough.                |
| `not_configured`   | soft           | No `workspace_agents` rows for this org.               |

Pre-save (user picked `remote_agent` but hasn't hit Save) the line shows a hint instead of polling — the backend would always answer `not_configured` until the ARN lands.

### Card 4 — Plugin health

`<PluginHealthCard>` iterates `usePluginsList()` (`GET /api/settings/plugins`, returns `PluginMeta[]` from the backend's discovery endpoint). Each row calls `usePluginHealth(plugin.id)` (`GET /api/${pluginId}/health`) and renders `{healthy, message}` with a badge and refresh timestamp. No hardcoded plugin list.

### Live updates

`useGithubInstallation` and `useOnboarding` poll every 5s — webhook-driven install lifecycle changes surface within one tick. No SSE for these (low frequency). `usePluginHealth` polls every 5s.

### Manifest-flow error banner

`<GhManifestBanner>` reads `?gh_manifest_error=...` from the URL — the manifest-callback endpoint redirects back with that query param on failure. The banner shows a one-line danger message above the GitHub card.

## Data owned

None. Mutations write through to plugin-owned endpoints.

## How it's tested

- `apps/e2e/tests/settings-cards-are-independent.spec.ts` — verifies any-order saves, no cross-card gating, plugin-health rows render.
- `apps/e2e/tests/onboarding-stepper.spec.ts` — exercises credentials-paste + install webhook dispatch.

Manifest happy-path can only be tested manually against real GitHub. The backend's `manifest-callback` has unit coverage in `apps/backend/app/plugins/github/test/`.
