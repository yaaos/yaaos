# core/observability

> OTel Web SDK initialization, identity stamping, and error-as-span capture for the yaaos SPA.

## Scope

Owns all browser-side OpenTelemetry concerns: SDK boot, span-processor identity stamping, error capture, and the `ErrorBoundary` wrapper. Does NOT own backend telemetry (that's `apps/backend/app/core/observability/`), log shipping, or any product analytics.

- **Receives:** collector endpoint + Dash0 auth/dataset from env vars in `main.tsx`; authenticated identity from `AppShell` via `useOtelIdentitySync`.
- **Emits:** OTLP spans to Dash0 via OTLP/HTTP with `Authorization: Bearer <token>` + `Dash0-Dataset` headers (all three fields must be set); `traceparent` on same-origin `/api/` fetches only (see `propagateTraceHeaderCorsUrls` in `sdk.ts`).
- **Hands to:** `core/api` — `traceparent` is injected at the global fetch layer, so `apiFetch` carries it without any client-side changes.

## Why / invariants

- **Triple-gated export.** Export requires `collectorEndpoint` + `authToken` + `dataset`. Any missing field → `NoopSpanProcessor`. SDK is always active (spans created, traceparent injected) so the backend always gets a parent span, even in dev with no collector.
- **Dash0 token is intentionally public.** `VITE_*` vars are inlined into the client bundle at build time. The token must be a Dash0 web-signal-restricted, dataset-scoped, ingest-only token — separate from the backend's token. This is a Dash0 design feature, not a security gap.
- **No baggage on the wire.** `yaaos.org_id`/`yaaos.user_id` are stamped as span attributes by `YaaosSpanProcessor.onStart` — client-side only. The backend stamps its own spans authoritatively from session context. Baggage would duplicate identity claims across a trust boundary; this design avoids that.
- **Backend stamps its own spans.** The client's identity attributes on web spans are a convenience for UI traces. The backend's `require(action)` and session middleware are the authoritative source on the backend side.
- **`traceparent` on same-origin `/api/` only.** `FetchInstrumentation` is configured with `propagateTraceHeaderCorsUrls` anchored to `window.location.origin/api/`. Cross-origin fetches (collector, CDN, third-party) never receive `traceparent`.
- **Collector host excluded from `FetchInstrumentation`.** `ignoreUrls` is set to the collector endpoint's origin so the tracer never traces its own export requests (avoids infinite-loop risk + noise).
- **Mutation errors land on spans.** `QueryClient` is constructed with `mutationCache: new MutationCache({ onError: recordException })` in `main.tsx` so all unhandled TanStack Query mutation errors are captured as span exceptions.
- **Global error handlers use `addEventListener`.** `_installGlobalErrorHandlers` registers via `addEventListener("error", ...)` and `addEventListener("unhandledrejection", ...)` so prior handlers compose naturally. `_resetObservabilityForTests` removes only the handlers it installed; pre-existing handlers are not touched.

## Public interface

Files under `core/observability/public/`, imported directly via `@core/observability/public/<file>`:

- `public/sdk.ts` — `configure(config)`, `recordException(err)`, `setIdentity`, `YaaosSpanProcessor`, `_resetObservabilityForTests()`.
- `public/error-boundary.tsx` — `<ErrorBoundary>` wraps the app tree; render errors → `recordException` → span exception event. Accepts an optional `fallbackRender` prop (receives `{ error, resetErrorBoundary }`) for callers that need a custom fallback (e.g. a retry button); `recordException` is still called regardless.
- `public/use-otel-identity-sync.ts` — `useOtelIdentitySync()` hook; called in `AppShell`; passively reads the `["auth","me"]` cache populated by `useCurrentUser` (via `UserCard`); never fetches (safe on pre-auth `/login` — empty cache → null identity); identity URL-scoped via `orgSlug`.

Private (non-`public/`): `identity.ts`, `span-processor.ts`.

## Modules

- `public/sdk.ts` — `configure(config)` initializes the provider; `recordException(err)` records on the active span (or opens a short-lived fallback span); `_resetObservabilityForTests()` for test teardown.
- `identity.ts` — module-scope identity holder (`setIdentity`, `getIdentity`). Read by `YaaosSpanProcessor.onStart`.
- `span-processor.ts` — `YaaosSpanProcessor` stamps `yaaos.org_id`/`yaaos.user_id` from the identity holder on every span start.
- `public/error-boundary.tsx` — `<ErrorBoundary>` wraps the app tree; render errors → `recordException` → span exception event. Optional `fallbackRender` prop for custom fallbacks; `recordException` fires regardless.
- `public/use-otel-identity-sync.ts` — `useOtelIdentitySync()` hook; called in `AppShell`; passive reader of the shared `["auth","me"]` cache via `useQuery({ ...currentUserQueryOptions, enabled: false })`; never fetches (safe on pre-auth `/login` — empty cache → null identity, no redirect risk); identity URL-scoped via `orgSlug` (null on `/login`); depends on `UserCard` mounting `useCurrentUser` on authenticated pages to populate the cache. See [`core_api.md`](core_api.md) for `useCurrentUser`.

## Gotchas

- **All `VITE_*` observability vars must be public-safe.** Vite inlines `VITE_*` env vars at build time — the value is embedded in the client bundle. `VITE_OTEL_COLLECTOR_ENDPOINT` must be a public-facing URL (not an internal hostname). `VITE_DASH0_AUTH_TOKEN` must be a web-signal-restricted, dataset-scoped, ingest-only Dash0 token — separate from the backend's token and intentionally designed to be embedded in browser bundles.
- **`VITE_ENVIRONMENT` ≠ `import.meta.env.MODE`.** Use `VITE_ENVIRONMENT` (set at deploy time, e.g. `production`, `staging`) for the `deployment.environment.name` resource attribute. `MODE` reflects the Vite build mode (`development`/`production`) and is not useful as a deployment environment signal.
- Call `configure()` exactly once, before `ReactDOM.createRoot()`. The provider registers globally; a second call is a no-op (guarded by `_provider !== null`).
- `setIdentity(null)` must be called on logout to avoid stale org/user attributes on spans after the session ends. `useOtelIdentitySync` derives identity from the `["auth","me"]` cache; when the cache is cleared (e.g. after logout invalidation), the hook re-renders and clears identity.
- `_resetObservabilityForTests()` must be called in `afterEach` for any test that calls `configure()` — it shuts the provider down, removes only our `addEventListener` error handlers, and clears the identity holder.
- Do NOT check `window.onerror` / `window.onunhandledrejection` in tests to verify handler installation — both stay `null`. The handlers are registered via `addEventListener`; dispatch a synthetic `ErrorEvent` to verify they fire.
- Source maps are emitted as `'hidden'` in Vite's build output — present on disk alongside the bundle but not referenced from the HTML. Upload to the symbolication service (Dash0) on deploy keyed by the release content hash. The maps are never served to the browser.

## Vocabulary

- **Identity holder** — module-scope `{org_id, user_id}` object in `identity.ts`; set after auth resolves.
- **Triple-gated export** — export only when `collectorEndpoint` + `authToken` + `dataset` are all truthy; SDK otherwise silent to the network (but still creates spans and injects `traceparent`).

## Entry points

- `apps/web/src/core/observability/public/sdk.ts` — `configure`, `recordException`, `_resetObservabilityForTests`.
- `apps/web/src/core/observability/public/error-boundary.tsx` — `ErrorBoundary`.
- `apps/web/src/core/observability/public/use-otel-identity-sync.ts` — `useOtelIdentitySync`.
- `apps/web/src/core/observability/test/observability.test.ts` — unit tests.
