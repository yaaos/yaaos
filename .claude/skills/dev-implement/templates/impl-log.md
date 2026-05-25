# impl-log — <slug>

Local-only. Records phase completions and controversial autonomous decisions. Resumption reads this file.

---

## Phase-complete entries

### Phase <N> — <phase goal>

- **Commit:** `<short SHA>`  <!-- or `(no changes — nothing to commit)` -->
- **Notes:** <one line if anything unusual; otherwise omit this line>

<repeat per phase>

---

## Autonomous decisions

<Only controversial or unclear ones. Obvious choices are not logged.>

### <one-line what>

- **Why:** <one line>
- **Where:** `<file:line>` <!-- if applicable -->
