# yaaos docs

Present-tense documentation for shipped code.

## System-wide

- [setup.md](setup.md) — operator setup.
- [system-architecture.md](system-architecture.md) — inter-app flows, cross-app conventions.
- [glossary.md](glossary.md) — shared vocabulary.
- [system-security.md](system-security.md) — security posture and threat model.

## Runbooks

- [runbooks/prod-deploy.md](runbooks/prod-deploy.md) — manual-config checklist + deploy flow + rollback for `app.yaaos.cloud`.
- [runbooks/secret-rotation.md](runbooks/secret-rotation.md) — rotating every secret yaaos depends on.

## Per-app

- [`apps/backend/docs/`](../apps/backend/docs/README.md) — FastAPI service.
- [`apps/web/docs/`](../apps/web/docs/README.md) — React SPA.
- [`apps/fake-github/docs/`](../apps/fake-github/docs/README.md) — test peer faking GitHub.
- [`apps/e2e/docs/`](../apps/e2e/docs/README.md) — Playwright suite.
