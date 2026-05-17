# Autonomous dev systems: dev-to-merge flow & HITL points

Reference note. How Stripe (Minions), Anthropic (Claude Code), and OpenAI (Codex) run "Slack-to-PR" coding agents. Captured to inform yaaos system design — especially where humans must sit in the loop.

## 1. The dev-to-merge flow

### Intake (Slack/CLI/bot → task)

- **Trigger**: Slack emoji/mention, CLI, web, or another bot (flaky-test detector, alert). Stripe's most common path is tagging `@minion` in a thread.
- **Spec extraction**: agent reads the thread/issue and drafts a structured task spec — goal, acceptance criteria, repo/scope.
- **"Enough requirements?" check**: agent self-asks against a checklist (target file/service known? success condition testable? scope bounded?). If unclear → replies in-thread with questions rather than guessing. **First HITL gate.**
- **Scoping rule**: only narrow, well-defined tasks accepted. Vague or cross-cutting → bounced. LLMs win on contained problems, lose on holistic codebase reasoning.

### Architecture / planning

- Agents do **not** make novel architectural decisions. They plan *within* existing patterns.
- Stripe's "blueprints": deterministic nodes (fetch context, codegen, run tests) wired around agentic nodes (understand, plan, write, interpret failures). Shape is hard-coded; only the fill is LLM.
- Context via internal MCP/tool layer (Stripe's Toolshed — ~500 tools; agent picks a task-relevant subset). Agent reads neighbor code/docs to mimic conventions.
- New module / new service / schema change implied → escalate. **Architecture changes are an HITL gate.**

### Code → PR loop

- Sandboxed devbox (warm EC2 pool, <10s spin-up). Branch, edit, run tests, read failures, fix, repeat.
- Bounded retries (N attempts) before escalating with a status comment.
- Opens PR with description, test results, and a confidence/risk summary.

### Review & merge

- **Stripe today**: every Minion PR gets human review. Autonomous to PR, human to merge.
- Auto-merge becomes plausible only when *all* signals green: passing tests, high coverage on changed lines, synthetic e2e green, blue/green deploy with cheap rollback, low-blast-radius area.
- **Hard HITL triggers**: irreversible ops, prod config, schema migrations, auth/security, payments/$ logic, infra/network, public API shape, breaking dep bumps.
- **Soft HITL triggers**: low test-confidence, agent retried >N, diff size over threshold, touched file flagged in sensitive registry, low semantic similarity to prior accepted PRs.

### HITL checkpoints — summary

1. **Intake** — requirements insufficient → ask in-thread.
2. **Plan** — task implies architecture change or out-of-scope edits → bounce.
3. **Mid-run** — N failed self-corrections → post status, wait.
4. **Pre-merge** — always human today; gated auto-merge only on green signals + low-risk path.
5. **Post-merge** — automated rollback on canary/blue-green regression. Not human, but the safety net that makes the rest tolerable.

### Variants: bugs and ops

- **Bugs**: intake = bug report / failing test / user complaint. Agent reproduces first (writes a failing test), then fixes. Same PR gate. Stripe runs flaky-test detection as a Minion trigger.
- **Ops/incidents**: intake = alert (elevated error rate, latency spike, log anomaly). Agent runs an **investigation phase** — pulls traces, logs, recent deploys, diffs — and posts a root-cause hypothesis to the incident channel. Hard HITL gate before any fix.
- Key difference: for ops, the **investigation output is the deliverable**, not code. Auto-remediation only for a small allowlist (revert last deploy, restart pod, scale up). Code/data changes still go through the normal PR path with a human at merge — blast radius of "agent fixes prod at 3am" is too high to skip review.
- Ops adds an **investigate → propose → human approves → execute** loop in front of the code-change loop. Everything else (scaffolding, scoping, merge gates) is identical.

### The pattern in one line

Narrow scope + deterministic scaffolding + LLM only inside the cells + cheap rollback + human at the merge button. The model isn't the moat; the **walls around the model** are.

## 2. System design — load-bearing details (often missed)

- **Codebase readiness is a prerequisite.** Agents are only as good as the test suite, type coverage, naming, and module boundaries they read. Messy codebase → messy PRs regardless of model. Stripe invested heavily here *before* Minions worked. The tach/modularity/docs discipline in yaaos isn't bureaucracy — it's agent infrastructure.
- **Memory / lessons across runs.** Top systems remember what worked, what got rejected, what reviewers flagged. Reusing that on the next task is most of the quality gain after week 1. yaaos's "lessons" concept is load-bearing, not decorative.
- **Tool curation, not tool pile.** Stripe has ~500 tools but each agent gets a task-relevant subset. Dumping every tool into context destroys reasoning. Pick per-task.
- **Permissions model = what the agent *cannot* do.** Read secrets, push to main, modify CI, touch infra, run prod commands. Define the deny-list explicitly; agent blast radius = union of its tools.
- **Observability on the agents themselves.** Acceptance rate, revert rate within 7 days, retry depth, time-to-PR, reviewer-edit-distance on merged PRs. Without these you can't tell if the system is getting better or worse.
- **Kill switches & rate limits.** Daily PR cap per area, per-author cap, global pause. A misconfigured agent can flood reviewers and erode trust overnight.
- **Trust ramp / task allowlist.** New agents start on low-stakes work (dep bumps, doc fixes, test additions, lint sweeps). Earn their way into higher-risk areas. Don't launch with "fix this auth bug" as task #1.
- **PR handoff quality is the product.** The reviewer's experience determines whether the system survives. Risk summary, what was tested, what was *not* tested, files touched outside obvious scope, confidence score — all in the PR body.
- **Reproducible, isolated environments are non-negotiable.** Stripe's warm devbox pool exists because a flaky env makes every agent run look broken. For yaaos: the workspace abstraction is one of the most load-bearing pieces.

## 3. The coding agent inner loop

What a good coding agent does between "task accepted" and "PR opened." Independent of intake (Slack, ticket, alert) and independent of merge gate.

### 0. Read the room before touching code

- **Identify the request type.** Feature / bug / refactor / docs / investigation. Each has a different inner loop. Misclassifying produces wrong-shaped output (a "bug" treated as a "feature" produces sprawl).
- **Locate the active milestone / scope contract.** What you're allowed to change is bounded by the project's stated scope, not by what looks improvable.
- **Read project-level instructions.** CLAUDE.md, contribution guides, the relevant module's docs. Honor them or argue for changing them first — never silently work around.

### 1. Intent reconstruction

- Restate the request in your own words. The user's words are evidence of intent, not intent itself.
- Surface the **unstated requirements**: success condition? what would make this a regression?
- List **ambiguities and assumptions**. For each: cheap to verify → verify; expensive → ask; trivial → state and proceed.
- **Stop and ask if intent is unclear.** 30-second clarifier beats 20 minutes of wrong code. Threshold: would a reasonable senior also be unsure? If yes, ask.

### 2. Ground-truth gathering

- Read the actual code that will change — don't work from memory.
- Read the **callers and neighbors** of the function you're modifying. Signature changes without checking callers = malpractice.
- Verify every named entity exists *now* (file, function, table, flag). Memory and old PRs lie.
- For bugs: reproduce first. A bug you can't trigger you can't fix.

### 3. Convention mining

- Before writing N lines, read 2-3 similar files. Mimic the style already there.
- Identify project idioms: error handling shape, naming, test layout, module boundaries.
- A new pattern requires a stated reason. "I preferred this" is not a reason.

### 4. Plan the smallest change

- Sketch the diff: which files, which functions, which tests.
- Ask: **what's the simplest change that makes the failing test pass / the bug go away / the feature work?** No surrounding cleanup. No "while I'm here." No premature abstraction.
- If the plan crosses module boundaries you didn't expect, stop — the scope or abstraction is wrong. Surface, don't push through.
- Estimate blast radius. High blast radius → smaller steps, more verification between them.

### 5. Test first (Red)

- Write the failing test that *encodes the intent*. Name reads like a requirement.
- Run it. Confirm it fails for the *expected reason* (not import error pretending to be failure).
- If a test isn't appropriate (config, doc), say so explicitly. Don't skip silently.

### 6. Minimum change (Green)

- Write only enough code to pass the test. No speculative branches, no error handling for impossible cases, no defensive validation at internal boundaries.
- Trust framework guarantees and internal invariants. Validate at system edges only.
- Match file style. No new pattern mid-file.

### 7. Self-review before declaring done

Against your own diff, in order:

- **Intent match**: does this satisfy what was asked, no more no less?
- **Scope check**: touched anything outside the task? Revert.
- **Dead code**: every new symbol used? Every removed thing fully removed (no stubs, no compat shims, no `// removed` comments)?
- **Comments**: any that just restate the code? Delete. Keep only non-obvious *why*.
- **Error handling**: any try/except that swallows? Any fallback for an impossible case? Delete.
- **Backward-compat hacks**: renamed `_unused`, re-exported types, deprecation aliases? Delete unless asked.
- **Root cause check**: fixed the symptom or the cause? If a test failed, fixed the bug or softened the assertion?
- **Bypassed a rule?** `--no-verify`, `# type: ignore`, disabled lint, skipped test? Undo and fix, or surface the conflict.

### 8. Run the real checks

- Lint, typecheck, tests, project CI scripts (in yaaos: `apps/<app>/bin/ci`).
- Read the output. "Looks like it passed" is not passing.
- Failures → fix the cause. Don't retry, don't skip, don't loosen.
- UI/frontend: actually open the feature in a browser. Type checks don't verify behavior.

### 9. Docs in the same commit

- For every symbol, route, payload field, table, concept you changed: grep docs and update each hit.
- `grep -rn "<old-thing>" apps/*/docs docs` should return zero before "done."
- If a rule moved, update the one canonical place — don't duplicate per-module.

### 10. Handoff — the PR is the product

The reviewer's experience determines whether the work ships. PR description must let them evaluate in under 2 minutes:

- **What changed and why** — two sentences.
- **What was tested** — and what was *not*.
- **Risk and blast radius** — files touched outside obvious scope, anything user-visible, anything in a sensitive area.
- **Confidence** — honest. "I'm not sure about X" is more useful than false certainty.
- **Open questions** — explicit, with proposed answers.

### Meta-rules across the whole algorithm

- **Stop and ask is a first-class action**, not a failure mode. Use on intent ambiguity, scope creep, conflicts with documented rules, irreversible operations.
- **Reversibility shapes caution.** Local edits: move freely. Pushes, force-pushes, deletes, prod changes, sent messages: confirm first.
- **Trust but verify your own memory.** Anything you "remember" about the codebase, re-check before acting.
- **Surface bad premises early.** If the task as stated doesn't make sense, say so before writing code. Doing the wrong thing well is the most expensive failure mode.
- **End-of-turn summary is short**: what changed, what's next.

## Sources

- Stripe Minions Part 1 — https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents
- Stripe Minions Part 2 — https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents-part-2
- ByteByteGo: How Stripe's Minions ship 1,300 PRs/wk — https://blog.bytebytego.com/p/how-stripes-minions-ship-1300-prs
- MindStudio: blueprint architecture (deterministic vs agentic nodes) — https://www.mindstudio.ai/blog/stripe-minions-blueprint-architecture-deterministic-agentic-nodes
- "The walls matter more than the model" — https://www.anup.io/stripes-coding-agents-the-walls-matter-more-than-the-model/
- InfoQ on Stripe agents — https://www.infoq.com/news/2026/03/stripe-autonomous-coding-agents/
- Claude Code best practices — https://www.anthropic.com/engineering/claude-code-best-practices
- Introducing Codex — https://openai.com/index/introducing-codex/
- HITL approval gates for coding agents — https://codeongrass.com/blog/how-to-build-human-in-the-loop-approval-gates-ai-coding-agents/
