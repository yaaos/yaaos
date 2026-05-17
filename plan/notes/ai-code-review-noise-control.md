# AI code review: controlling nitpick noise

Reference note. How CodeRabbit, Greptile, Augment, Claude Code Review, and others keep AI reviewers from flooding PRs with low-value comments. Captured to inform yaaos's reviewer module.

## Core techniques (in roughly the order tools adopt them)

- **Severity schema enforced in the output format.** Comment must classify itself: `blocker | major | minor | nit`. Drop everything below a configured tier before posting. Tools that *require* the schema (no comment without a category) drop noise ~60% on the spot.
- **Explicit "do NOT flag" list in the system prompt.** Style nits, missing semicolons, "consider renaming," speculative risks with no concrete failure path, broad architectural opinions, intentional behavior changes, anything the linter/compiler already catches. Single highest-leverage prompt section.
- **"Prove it or discard it" rule.** Comment must describe a concrete failing scenario — inputs, code path, observed-vs-expected. Vague "this could break" findings dropped. Forces the model past pattern-matching.
- **Confidence threshold filter.** Greptile-style: each finding gets 0–100. Repos set a floor (e.g. 85 on critical paths, 70 on experiments). Caveat: calibration is imperfect — false positives at 90, real bugs at 60. Coarse knob, not a solution.
- **Defer to tooling.** Lint, typecheck, formatter, security scanners run *first*. AI reviewer is told what those already flagged and is forbidden from re-flagging. Every token spent on "unused import" is a token not spent on real analysis.
- **Context perspective in the prompt.** "This service runs behind 3 layers of auth, batch job, latency-insensitive." Without scoping, the model reviews from every angle and generates noise about non-issues. Embed the service's reality in the prompt.
- **Cap on findings per PR.** Hard limit (e.g. top 5 by severity × confidence). Forces ranking. A 30-comment review gets ignored regardless of quality.
- **De-duplication across comments.** Same root issue in 4 files → one comment with file list, not 4.
- **Suppress comments on lines outside the diff.** Restrict scope unless the diff causally implicates an off-diff line.
- **Learning loop from reviewer feedback.** Resolved-without-edit = negative signal; comments that led to a code change = positive. Feeds per-repo learned config (CodeRabbit's approach).
- **Author-controlled review depth.** PR labels or commands (`/review --strict`, `/review --quick`) let humans pick noise budget per PR. Refactors get more depth than dep bumps.

## Benchmark numbers — the inherent tradeoff

- **Greptile**: ~82% bug catch, ~60% of comments are nits or false positives. High recall, low precision.
- **CodeRabbit**: ~44% catch, ~2 false positives per PR. Lower recall, higher precision.
- Pick a point on the curve **per repo**, not globally.

## Output-quality target

≥60% of comments must be Tier 1 (would cause prod failure) or Tier 2 (real maintainability issue). If not, the reviewer is a noise generator and developers will mute it within 2 weeks. This is the **eval metric for the reviewer itself**.

## Implications for yaaos reviewer

- Reviewer prompt needs an explicit **"do NOT flag"** section, not just a "what to look for" section. Negative list is more load-bearing than positive list.
- Per-finding **severity + concrete-failure-scenario** schema. Reject malformed findings before they reach the PR.
- Eval loop: track resolved-without-edit rate per finding type. That's how the prompt gets tuned over time.
- Linters/typecheck/security scanners run *before* the reviewer and feed it an "already-flagged" list to skip.
- Per-PR cap on findings. Force ranking.

## Sources

- Augment: how we built a high-quality AI reviewer — https://www.augmentcode.com/blog/how-we-built-high-quality-ai-code-review-agent
- Augment: benchmarking 7 AI reviewers — https://www.augmentcode.com/blog/we-benchmarked-7-ai-code-review-tools-on-real-world-prs-here-are-the-results
- PhotoStructure: most AI code reviews are noise — https://photostructure.com/coding/claude-code-review/
- Propel: reducing false positives — https://www.propelcode.ai/blog/ai-code-review-false-positives-reducing-noise
- Jet Xu: signal-vs-noise framework — https://jetxu-llm.github.io/posts/low-noise-code-review/
- Greptile vs CodeRabbit benchmark — https://dev.to/rahulxsingh/coderabbit-vs-greptile-which-ai-reviewer-catches-more-bugs-4n9k
- Gitar: 7 tactics to cut false positives — https://cms.gitar.ai/reduce-false-positives-code-review/
