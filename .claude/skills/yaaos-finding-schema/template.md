# Finding render template

> Shared template applied at output time by the yaaos-review pipeline's entry skills (`yaaos-review`, `yaaos-review-pr`). The orchestrator does NOT pre-render text into `final.json` — every renderer reads structured fields and applies this template fresh.

## Two flavors

The template has one structural variant: the **Code** block (file:line header + fenced source snippet) is included in stdout-flavor and **omitted** in PR-flavor.

- **Stdout-flavor** (`yaaos-review`): the reader is in their terminal and has no other context anchoring them to the file. Include the Code block.
- **PR-flavor** (`yaaos-review-pr` line comments + issue comments): the comment is already anchored to the file:line by GitHub's review UI. The Code block is redundant noise — omit it.

Everything else is identical.

## Field sourcing

All values come from the finding object emitted in `final.json`:

- `<category>` ← `finding.category`
- `<severity>` ← `finding.severity`
- `<confidence>` ← `finding.confidence`
- `<id>` ← synthesized at render time: `<category-prefix>-NNN` zero-padded to 3 digits, numbered within the finding's category in `final.json`'s sort order. Prefixes: `security` → `sec`, `code` → `code`, `architecture` → `arch`.
- `<headline>` ← first sentence of `finding.rationale`, derived at render time (split on the first `. ` or newline; strip trailing period). If `rationale` is a single sentence, use it whole and leave the Problem body empty.
- `<problem-body>` ← remainder of `finding.rationale` after `<headline>` is removed. If empty, omit the prose line entirely (keep the **Rule violated** line).
- `<rule_violated>` ← `finding.rule_violated`, verbatim.
- `<file>` ← `finding.file`
- `<line>` ← `finding.line`
- `<snippet>` ← read at render time from `<file>` at `<line>` (one line of context, or a short block if the offending construct spans multiple lines). Stdout-flavor only.
- `<suggested_fix>` ← `finding.suggested_fix`, verbatim.

## Template

The template is shown below inside a **four-backtick fence** so the inner triple-backtick code fence (around `<snippet>`) reproduces correctly. When rendering, emit only the inner content — three backticks around the snippet are part of the output.

````
**yaaos-<category>**

**<id> [<severity> · <category> · <confidence>]**
<headline>

### Problem
<problem-body>

**Rule violated:** <rule_violated>

<!-- stdout-flavor ONLY: include the Code block. PR-flavor omits this block entirely. -->
**Code:** <file>:<line>
```
<snippet>
```

### Suggested fix
<suggested_fix>

<sub><code><id></code></sub>
````

## Notes

- `<rule_violated>` is never paraphrased anywhere in the pipeline — copy it verbatim from the finding.
- Both `<headline>` and `<problem-body>` come from `rationale`, which the Wave 4 restate pass has already rewritten in plain peer-engineer language. The renderer does no further rewriting.
- The footer uses `<sub>` so GitHub renders it small (PR-flavor) and `<code>` so the id reads as a monospace tag visually distinct from the surrounding small prose. On a terminal both tags degrade to plain text. Keep both wrappers in both flavors so a reader can grep a finding id back to its tally.
