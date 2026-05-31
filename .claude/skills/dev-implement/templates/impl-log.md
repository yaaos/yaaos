# impl-log — <slug>

Local-only. Records per-phase completions (or failures) and any autonomous decisions the subagent made during that phase. Resumption reads this file to find the last completed phase block. CI logs live alongside as `.ci-phase-<N>.log`.

---

### Phase <N> — <phase goal>

- **Commit:** `<short SHA>`  <!-- or `(no changes — nothing to commit)`, or `(failed — see .ci-phase-<N>.log)` -->
- **Summary:**
  - <one-line bullet per meaningful file or test added>
- **Autonomous decisions:** <!-- omit this whole field if empty -->
  - **<one-line what>** — why: <one line>; where: `<file:line>` <!-- where is optional -->
- **Notes:** <!-- deferred-not-fixed observations the subagent surfaced; omit this whole field if empty -->
  - <one terse line per deferred item>

<repeat per phase>
