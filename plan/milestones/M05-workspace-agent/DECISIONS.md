# M05 — decisions made during autonomous run

> Append-only log of decisions made when the spec was ambiguous and certainty was below 3 of 5. Per [START_HERE.md § Decision protocol](START_HERE.md#decision-protocol).

## Format

Each entry:

```
### <Phase N> — <one-line decision summary>

- **Certainty**: <1 or 2>/5
- **Decision**: <what was chosen>
- **Alternatives considered**: <brief>
- **Why this one**: <one line>
- **Reversal cost**: <low/medium/high — how painful to undo later>
```

Keep entries terse. The user reads this at the end of the run; volume = friction.

## Entries

<!-- Append below. Do not edit prior entries. -->

### Phase 9 — image registry: GHCR with semver+latest+sha tagging

- **Certainty**: 2/5
- **Decision**: Publish the WorkspaceAgent image to GitHub Container Registry (`ghcr.io/yaaos/yaaos-agent`). Tag strategy: each release tagged `vX.Y.Z`, the latest stable also re-tagged `latest`, every CI build tagged `sha-<short>` for traceability. Customers consume the immutable `vX.Y.Z` tag in their ECS task definition; `latest` exists for getting-started flows.
- **Alternatives considered**: (a) Docker Hub — wider familiarity but rate-limited pulls hit customer ECS task launches; (b) ECR Public — AWS-native but requires customers to authenticate the otherwise-public-image case; (c) per-customer-private registry — too much friction for a free OSS-first POC.
- **Why this one**: GHCR is free for public images, has no anonymous pull rate limits, ships with GitHub Actions, supports OCI image manifests for multi-arch (`linux/amd64` + `linux/arm64`). Migrating is a registry rename if we ever change our minds.
- **Reversal cost**: low — image URL lives in ECS task definitions; customers re-pin on next deploy.

### Phase 8b follow-on (slice 79) — extend `subscribe` payload with `workflow_execution_id`

- **Certainty**: 2/5
- **Decision**: `SubscriberRegistry` emits `{type: "subscribe", workspace_id, workflow_execution_id}` (both IDs). Agent caches the `workspace_id → workflow_execution_id` mapping at subscribe time so outbound `activity_batch` frames carry the right workflow id keyed by the workspace id it knows.
- **Alternatives considered**: (a) Backend resolves `workspace_id → current_holder_workflow_id` from `WorkspaceRow` on every inbound `activity_batch`. (b) Send `subscribe` keyed solely on `workflow_execution_id`. (c) Add `workflow_execution_id` to `AgentCommand` so the agent knows it from claim time.
- **Why this one**: (a) hits an asyncpg cross-loop issue under TestClient when the seeded data lives in the outer-test event loop and the WS handler runs in BlockingPortal's loop — pivoted away after the first attempt. (b) breaks the agent's existing workspace_id-keyed `SubscriptionSet`. (c) is a larger refactor of the claim contract for one keying concern. Extending the subscribe payload is local + cheap.
- **Reversal cost**: low — bump the protocol once both halves drop the field.

### Phase 8b follow-on (slice 82) — `github.com/coder/websocket@v1.8.13` for the agent's WS client

- **Certainty**: 3/5
- **Decision**: Adopt `github.com/coder/websocket` (the maintained fork of the archived `nhooyr.io/websocket`) for the agent's bidirectional activity stream. Pin to v1.8.13 — v1.8.14+ requires Go 1.23 and the agent stays on Go 1.22 per the Dockerfile.
- **Alternatives considered**: (a) `gorilla/websocket` — long-standing standard but heavier. (b) `nhooyr.io/websocket` — archived. (c) Hand-roll RFC 6455 over `net/http` — way too much work for a POC.
- **Why this one**: Context-first API matches the agent's existing context discipline; small dependency footprint (3 indirect deps); active maintenance under the coder fork; tests use the same package's `Accept` for server-side — no extra fixture cost.
- **Reversal cost**: medium — `gorilla/websocket` swap is mechanical (similar API) but every Read/Send/Close site needs touch-up; pinned v1.8.13 means a future Go-toolchain bump is the natural trigger to revisit.

### Phase 0b — split `014_create_all_m05` into per-phase migrations

- **Certainty**: 3/5
- **Decision**: Phase 0b's migration `014_create_outbox_entries` creates only the outbox_entries table. Subsequent M05 phases add their own migrations (`015_*`, `016_*`, …) as their owning module's model lands.
- **Alternatives considered**: (a) Write all new model files now with placeholder bodies + one `014_create_all_m05` migration; (b) Defer all M05 migrations until every model is ready.
- **Why this one**: (a) would leak future-phase columns into scaffolding before they're designed, contradicting the per-phase build order; (b) leaves outbox_entries un-creatable on existing DBs, breaking `core/tasks` tests. Per-phase migrations match how M01–M03 actually shipped (multiple migrations per milestone).
- **Reversal cost**: low — migration registration is a tuple append; future migrations can consolidate if desired.
