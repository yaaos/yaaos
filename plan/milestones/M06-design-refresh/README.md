# M06 — Design refresh + IA rework

> Re-design the yaaos SPA from first principles. Heavy focus on information architecture and UX flows (this is a devops tool — lots of information, must be logical and uncluttered). Visual style: modern, clean, crisp — not differentiating. Adopt a standard React component library rather than continuing to hand-roll primitives.

## Status

`[planned]` — early planning. Sequenced after M05. We are walking through [process.md](process.md) one section at a time to fill in the rest of the milestone's planning docs.

## How this milestone is being planned

Unlike M03–M05, M06 is mostly a design exercise. The owner (Jack) is a backend / devops engineer, not a designer, so the plan is built by structured conversation: we walk through [process.md](process.md) section by section, lock decisions as we go, and persist the outputs into the docs below as they firm up.

## Reading order

### For planning context (how we got here)

1. **[process.md](process.md)** — the meta plan. What sections we worked through, in what order, and what each one produced.

### For execution (what to ship)

2. **[START_HERE.md](START_HERE.md)** — autonomous-execution entry point. Read first if you're picking M06 up cold.
3. **[requirements.md](requirements.md)** — the locked product spec, A1 → F2. The source of truth.
4. **[api-changes.md](api-changes.md)** — per-surface REST API diff. What's new, renamed, extended, deleted.
5. **[PHASES.md](PHASES.md)** — the 9-phase implementation order with per-phase definition of done.

`architecture.md` and `implementation-plan.md` are intentionally not written — between requirements.md, api-changes.md, and PHASES.md, the territory is fully covered. A separate `DECISIONS.md` is also unnecessary: every "why" is captured inline at the point of the decision.

## Drivers (why now)

- Product understanding is mature enough to do IA + UX properly. Earlier milestones laid tracks; M06 reflects on the whole layout.
- Current SPA is functional but demo-grade. Credible look needed before showing customers.
- M05 adds new surfaces (workflow execution, activity stream, workspace status) that were built fast and need proper design alongside everything else.
