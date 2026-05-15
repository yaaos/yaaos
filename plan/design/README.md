# Handoff: yaaof — M01 UI

> Self-hosted, team-scale agent orchestration service.
> Frontend prototype + spec for the M01 surfaces: Dashboard, Tickets list, Ticket detail (Review + Audit log), Memory, Prompts, Repos, Settings.

---

## ⚠️ Precedence — read before implementing

This folder is **visual + interaction reference**, not authoritative spec. When implementing M01, follow this precedence:

1. **Planning docs** (`../milestones/M01-code-review/`) — the source of truth. Module maps, internals, data model, requirements, patterns, modularity. If anything in this folder contradicts the planning docs, the planning docs win.
2. **[M01-DELTAS.md](M01-DELTAS.md)** — locked deviations from the design prototype below. **Read this immediately after the planning docs and before reading anything else in this folder.** It overrides specific design decisions (dropped features, hardcoded placeholders, scope changes).
3. **This folder** (the design output) — visual reference for spacing, color, typography, motion, and the *shape* of interactions. Use it to make screens look and feel right.

In practical terms: if the planning docs say "no `applied_count` field on Lesson" and the design's mock data shows `applied_count: 217`, the planning docs win — drop the counter. If `M01-DELTAS.md` says "no `View prompt` button," remove it from your implementation even though the prototype renders one. The deltas + planning docs are authoritative; the design files are reference.

---

## About the design files in this bundle

Everything in this bundle is **design reference**. The HTML prototype is a fully-interactive React 18 + inline-Babel mock that shows the intended look, motion, and behavior — it is **not the code you should ship**. Your task is to recreate the screens (modulo the deltas) inside the target codebase (React 18 + Vite + TypeScript + TanStack Router + TanStack Query + Tailwind + shadcn/ui + lucide-react + sonner + zod + react-hook-form, per the planning docs) using its established patterns.

Three artifacts live here:

```
plan/design/
├── README.md            ← this file (visual reference)
├── M01-DELTAS.md        ← locked deviations from the prototype (read first)
├── yaaof.html           ← high-fidelity clickable prototype
├── app/                 ← prototype source (CSS tokens, JSX components)
└── wireframes/          ← layout exploration (lo-fi) — for context / rationale
```

Use **`yaaof.html`** as the visual reference for spacing, color, typography, motion, and interaction shape. Treat **`M01-DELTAS.md`** as the corrections layer that turns the prototype into the actual M01 spec. The wireframes are kept only to explain *why* the layouts ended up the way they did.

---

## Fidelity

**High-fidelity (hifi).** Pixel-perfect mockups with final colors, typography, spacing, micro-interactions, and live motion. Recreate them faithfully using shadcn/ui + Tailwind.

The prototype is structured as plain JSX so component mapping to shadcn primitives is direct:

| Prototype element | shadcn equivalent |
|---|---|
| `.card` / `.card-h` / `.card-b` | `<Card> / <CardHeader> / <CardContent>` |
| `.btn` / `.btn-primary` / `.btn-ghost` / `.btn-danger` | `<Button variant="default | primary | ghost | destructive">` |
| `.badge` (+ tone modifiers) | `<Badge variant="...">` |
| `.tabs` / `.tab` | `<Tabs> / <TabsList> / <TabsTrigger>` |
| `.modal` / `.scrim` | `<Dialog> / <DialogContent>` |
| `.toast` | `sonner` toasts (already in stack) |
| `.input` / `.textarea` | `<Input> / <Textarea>` |
| `.input` + dropdown chevron | `<Select>` |
| `.search` | `<Command>` palette wrapper |
| `.chip` | small `<Badge variant="outline">` |
| `.sb-item` / `.sb-rail-item` | hand-roll inside a `<aside>` — see Sidebar below |

Use **lucide-react** for every icon. The prototype's hand-rolled icons in `app/icons.jsx` are 1:1 mappings to lucide's named exports (Dashboard → LayoutDashboard, Tickets → Ticket, Brain → Brain, Repo → GitBranch, Settings → Settings, GitHub → Github, etc.).

---

## Top-level information architecture

Six left-nav routes:

1. `/dashboard` — landing page; populated state + onboarding empty state
2. `/tickets` — list; nested `/tickets/:id/review` and `/tickets/:id/audit`
3. `/memory` — per-repo lessons
4. `/prompts` — per-agent prompt editor
5. `/repos` — allowlist
6. `/settings` — GitHub App + API key + plugin health

The current hash-based routes in the prototype map directly to TanStack Router routes:

```
/dashboard
/tickets
/tickets/$ticketId
/tickets/$ticketId/audit         (Review tab is the index)
/memory
/memory/$repoId                  (deep link to repo tab)
/prompts
/prompts/$agentId                (deep link to agent tab)
/repos
/settings
```

No auth in M01. No org switcher. No user menu. No global search (each list has its own filter).

---

## Design tokens

All colors are **oklch** with a 1° violet bias on neutrals so dark + light tones share a hue family. Light mode is OS-default detectable but users can override via a top-bar toggle, which persists.

### Color tokens (drop into `tailwind.config.ts` extends → colors or as CSS vars)

```
/* Dark (default) */
--bg:          oklch(0.165 0.012 285)
--bg-2:        oklch(0.195 0.013 285)
--surface:     oklch(0.215 0.013 285)
--surface-2:   oklch(0.248 0.014 285)
--surface-3:   oklch(0.282 0.014 285)
--hover:       oklch(0.265 0.016 285)
--border:      oklch(0.305 0.014 285)
--border-soft: oklch(0.250 0.013 285)
--border-hard: oklch(0.385 0.018 285)

--text:        oklch(0.965 0.005 285)
--text-2:      oklch(0.770 0.012 285)
--text-3:      oklch(0.575 0.014 285)
--text-4:      oklch(0.460 0.014 285)

/* Accent — electric violet (default) */
--accent:        oklch(0.72 0.19 295)
--accent-2:      oklch(0.80 0.16 295)
--accent-dim:    oklch(0.46 0.14 295)
--accent-bg:     oklch(0.30 0.10 295 / 0.30)
--accent-bg-2:   oklch(0.30 0.10 295 / 0.14)
--accent-border: oklch(0.50 0.16 295 / 0.55)

/* State colors */
--success:      oklch(0.74 0.17 150)
--danger:       oklch(0.70 0.20 25)
--warning:      oklch(0.80 0.16 75)
--info:         oklch(0.74 0.13 235)
```

Light mode mirrors these — see the `[data-theme="light"]` block in `app/yaaof.css` for exact values. Switch by setting `data-theme="light"` on `<html>`.

### Typography

- **Primary font**: `Geist` (Google Fonts) — weights 400/500/600/700
- **Mono font**: `Geist Mono` — weights 400/500/600
- **Body size**: 13px (`html { font-size: 13px }`)
- **H1**: 20px / 600 / −0.012em letter-spacing
- **Sec-h** (small all-caps section labels): 10.5px / 600 / 0.08em uppercase
- **Mono numerics**: always with `font-variant-numeric: tabular-nums`
- Feature settings: `"ss01", "cv11"`

### Spacing

Tailwind's default 4px scale is fine. The prototype uses these CSS custom-property scales:

```
--d-row: 38px (regular) / 32px (compact) / 44px (comfy)
--d-pad: 16px (regular) / 12px (compact) / 20px (comfy)
```

Density is a user-tweakable preference (Tweaks panel). Set on `<html data-density="...">`.

### Radii

`4 / 5 / 6 / 8 / 10 / 999` px. Buttons + inputs use `6`. Cards use `10`. Chips use `4`. Badges use `999`.

### Shadows

```
--shadow-sm:   0 1px 2px oklch(0 0 0 / 0.35)
--shadow:      0 6px 24px oklch(0 0 0 / 0.36), 0 1px 2px oklch(0 0 0 / 0.22)
--shadow-lg:   0 20px 60px oklch(0 0 0 / 0.5)
--shadow-glow: 0 0 0 1px var(--accent-border), 0 0 24px oklch(0.50 0.16 295 / 0.35)
```

`--shadow-glow` is used on *running* agent cards to telegraph that "this card has a live job inside it".

---

## Layout shell

```
┌─ Sidebar (220px pinned / 48px floating-rail) ─┐ ┌─ Main ───────────────────────┐
│ • Logo placeholder                            │ │ Topbar (44px)                │
│ • WORKSPACE section header                    │ │  · breadcrumbs · live pill   │
│ • 6 nav items, with optional count chips      │ │  · search · ⌘K · theme tog.  │
│ • Footer: version + pin/unpin button          │ │ ────────────────────────     │
└───────────────────────────────────────────────┘ │ Content (scroll-y)           │
                                                  │  · .page (max-width 1320–    │
                                                  │           1500 by route)     │
                                                  └──────────────────────────────┘
```

### Sidebar pin / float behavior

This is a **single primitive with two user-controlled states** — like Linear / VSCode / Slack.

- **Pinned** (default): 220px column, all labels visible. Pin button in footer is "active".
- **Floating** (unpinned): collapses to a 48px icon-only rail. Hover or click any rail icon → the full 220px panel slides out, **overlaying content** (not pushing it). Mouse-leave closes the panel. Clicking any item closes the panel + navigates. Pin button in the panel footer re-pins.

Persistence: store `sidebarPinned: boolean` in local user prefs. The rail is **always visible** so navigation is always one click away.

Active-route indicator: a 2px accent-violet bar on the left edge of the active item + a faint accent-bg tint.

A live count chip appears on the Tickets nav item when there are tickets in review. In rail mode it becomes a small accent-violet dot in the top-right corner of the icon.

---

## Screens

### 1. Dashboard (`/dashboard`)

Two states based on `onboarding` config:

**Onboarding empty state** (any of `github_app | api_key | repos` is false):

- H1 "Welcome to yaaof", sub "Three steps to your first review", right-aligned `0 of 3 complete` badge.
- A single Card containing 3 stepper rows:
  1. Install the GitHub App
  2. Add your model API key
  3. Add a repo to the allowlist
- Each row: 28px circular numbered avatar (filled green when done) → text block (title + sub) → primary "Install" / "Add key" / "Add repo" button on the right. Completed rows get a `--success-bg` tint and a struck-through title in `--text-3`.
- Below: small auxiliary Card with a paragraph explaining what happens after onboarding.

**Populated state** (all 3 booleans true):

- Page header: H1 "Overview", sub "acme · last 24h"; right side: `acme` badge + `Last 24h ▾` button.
- **Metrics row** — 5 equal-flex tiles in a single row:
  1. Reviews 24h — large mono number + sparkline (gradient-fill area chart, 22px tall)
  2. Avg latency — `3m 04s` mono + green delta `−8s`
  3. Cost 24h — `$4.82` mono + delta `+$0.41`
  4. Open tickets — bare number + sub "in review"
  5. Queue · workers — `1 · 2/4` mono + sub "0.0s wait p50"
- **Lower split**:
  - Left (flex 1): **Live agents · in flight** card. Each in-flight ticket is a row showing `#number repo title` + `updated 38s ago` on first line; second line shows `arch · sec · style` agent labels each followed by an inline state element (running → indeterminate bar; queued → grey square + "queued"; verdict → small badge). This is the **live spectacle** — running bars animate continuously.
  - Right (fixed 380px): **Activity** card. Reverse-chronological feed with severity-colored 2px square + message + relative timestamp. Most recent row gets a 1.8s `flash-new` accent-bg fade-in.

### 2. Tickets list (`/tickets`)

- Page header: H1 "Tickets", sub `acme · N in review · M done`. Right: search box (`/` shortcut hint), "Sort · newest ▾" button.
- **Filter bar**:
  - Left: All / Review / Done count chips + `repo`, `kind`, `author` dropdown badges
  - Right: `group` segmented control with `None | Status`
- **Default (None)**: One Card with a dense table. Columns (CSS grid):

  ```
  78px 1.7fr 110px 88px 70px 28px 130px 60px 64px
  Status  Ticket Kind Review Cost Src Author Tokens Updated
  ```

  Each row is a Link. Status column hosts a Review/Done Badge. Ticket column stacks `#NNNN repo` on top of the title. Review column shows 3 small (9px) colored squares representing each agent's verdict (green=approved, red=changes, grey=comment, dashed-outline=skipped, **pulsing dot=running**). Live tickets show a pulse-dot to the right of the verdict dots and a violet live-counting token total. Hovering shifts background to `--hover`; row click navigates to `/tickets/$id`.

- **Group · Status**: Same row primitive but the leading Status column is hidden — section headers (`Review · N` accent-violet, `Done · N` success-green) act as the column. Review section shows "updates live · last change Ns ago" in the section header strip.

### 3. Ticket detail · Review tab (`/tickets/$id/review`)

**Header**: stacked
- Tiny mono row: `#NNNN · repo` in `--text-4`
- H1 ticket title
- Chip row: Status badge (`Review`) + Kind chip (`new feature`) + optional `draft` chip
- **Source line**: a sentence-style row that generalizes beyond GitHub PRs. Sec-h "Source" + source-icon (`PR` glyph) + `PR #NNNN on acme/web` + author avatar/name + opened/merged + relative time + `head → base` (mono) + `+247 −38 in 11 files` (additions green, deletions red) + dashed-underline "open in GitHub ↗" link.
- Right side: `Cancel jobs` button + primary `Re-review` button.

**Tabs** below header: `Review (3)` / `Audit log (10)` — count is total agents / total entries.

**Review tab body**:

1. **Summary strip card**: 5 equal cells in a row (Findings, Total cost, Tokens, Latency, Lessons). Findings cell turns danger-red when there's a `must-fix`.
2. **Agent cards**, one per agent (`arch / sec / style`), each a Card containing:
   - Card header: agent avatar (rounded violet square, single letter), agent name, mono chip `agent · $id`, prompt hash + model + lessons-applied count in `--text-3`.
   - Right side of header: state badge (Running = accent w/ pulse dot, Queued = neutral, Posted = Approved/Changes/Comment) + `View prompt` button.
   - **If Running**: card border becomes accent + `--shadow-glow`. Body shows the current step ("Invoking coding agent (claude-code)" etc.), an indeterminate progress bar, and a row of mono live counters: elapsed, tokens in/out (ticking up), cost (ticking up). Updates every 500–1500ms.
   - **If Queued**: spinner + "Queued · waiting for worker slot" + worker pool status.
   - **If Posted**: meta row with posted-time, duration, tokens, cost + "view on GitHub ↗".
   - **Findings list** — see below.

**Findings (collapsible row)**:
- Collapsed: severity dot (red/amber/blue/grey) + bolded title + severity chip + mono `file:line` + 1-line body + caret.
- Expanded (preserve user-clicked one in URL or local state — for the prototype, the second style finding `f2` is open by default): full body paragraph + **code snippet** (mono code block with line numbers; added lines green-tinted, removed lines red-tinted, prefix `+`/`−`/space) + agent rationale in italic quotes + **applied-lesson chip** if any (accent-bg with Sparkle icon, lesson title linked to /memory) + action row: `Resolve` (primary), `Reply on GitHub` (secondary), `Dismiss` (ghost), `Teach yaaof…` (ghost). The last opens a "New lesson" modal in Memory pre-filled with the finding context. Right-aligned "view on GitHub ↗" dashed link.

### 4. Ticket detail · Audit log tab (`/tickets/$id/audit`)

- Filter chips: `All N` + one chip per kind-prefix (`review_job · N`, `lessons · N`, `ticket · N`). Active filter is accent.
- Table card with columns: `[2px tone dot] When  Kind  Summary  Actor  Cost`.
  - Tone dot is colored by kind: `review_job.posted` → success (or danger if CHANGES_REQUESTED), `review_job.prompt_sent / step_changed / ticket.created` → accent, else neutral.
  - Kind in mono semibold; summary in mono `--text-2` (auto-generated one-liner derived from payload — see `summarize()` in the prototype).
  - Actor: avatar (system/agent/github_user variant) + name.
  - Cost: USD value if present in payload, else `—`; trailing chevron rotates 180° when row is expanded.
- Click any row → expands to an inline collapsible code block showing the full Pydantic payload as pretty JSON (background `--bg-2`, mono code, line-height 1.65). Only one row open at a time.
- The newest row on initial load flashes once with `flash-new` animation.

### 5. Memory (`/memory`)

- H1 "Memory", sub "Per-repo lessons applied to every review on that repo." Right: primary `+ New lesson` button.
- Repo tabs as accent/soft badges with mono name + lesson count.
- Below the tabs, an info line clarifying that lessons for this repo are added to the prompt for every review.
- Lesson list — one Card per lesson:
  - Title in 14px / 600.
  - Right-side metadata: `from #NNNN`, `added Nd ago`, `applied N×` (mono), Edit and Trash icon-buttons.
  - Body paragraph below.
- Empty state when a repo has no lessons: centered title + sub + `+ Write the first lesson` primary button.

**New lesson modal** (`<Dialog>` in shadcn):
- Header: "New lesson" + `in acme/web` tag + close button.
- Title input.
- Body textarea with `maxLength=1000` and a live `N / 1000` counter (counter turns red below 100 left).
- Footer: `Cancel` + primary `Save lesson` (disabled until both filled).
- On save: toast "Lesson "Title" added to repo".

### 6. Prompts (`/prompts`)

- H1 "Prompts", sub "3 built-in review agents · prompts editable · agent set is fixed in M01."
- Tabs across the agents (Architecture / Security / Style). A tab gets an accent-violet dot when its prompt is dirty.
- Meta row under tabs: `prompt-hash`, `updated 3d ago by you`, `applied to N reviews`. Right side: `Reset to default` button (confirm) + primary `Save` (disabled when not dirty).
- Single big Card containing:
  - Tiny header strip: `prompt · markdown` (left) + `N chars · N lines` (right), background `--bg-2`.
  - A single `<Textarea>` filling the card (min-h 480px), `font-family: 'Geist Mono'`, font-size 12px, line-height 1.65, no border or focus ring inside the card.
- Footer line in `--text-4`: "Saved prompts apply to the next review. In-flight reviews use the prompt snapshotted at job start."

### 7. Repos (`/repos`)

- H1 "Repos", sub "Allowlist · yaaof opens a ticket when a PR lands on any of these."
- Add-repo bar: a Card with `+` icon + `owner/name` input + `Verify access` button + primary `Add repo` button (disabled unless input contains `/`).
- Repos table (Card with sticky header):

  ```
  Columns: Repo · Language · Status · Lessons · Last review · (actions)
  ```

  - Repo: github-icon + mono name
  - Status badges: `active` (success), `install missing` (danger), `unreachable` (danger)
  - Lessons: dashed-underline link to /memory with count
  - Last review: relative time (or `never`)
  - When status ≠ active: a `Reconnect` button appears in the actions column

### 8. Settings (`/settings`)

- H1 "Settings", sub "M01 has no auth. Single org · single self-hosted install."
- Three stacked Cards:
  - **GitHub App** — icon + title + `installed` success badge + 3 mono meta facts + `Manage on GitHub ↗` + `Reinstall` buttons.
  - **Model API key** — icon + title + `configured` success badge + provider + key preview + added-date + `Rotate key` + `Test connection` (toasts "Connection OK · 412ms latency").
  - **Plugin health** — icon + title + auto-incrementing "refreshed Ns ago" mono caption. Rows for `github / anthropic / claude-code / sse`, each with a healthy success badge and right-aligned latency/client count + "checked Ns ago".

---

## Cross-cutting interactions

### Sidebar
- Click pin button in footer → toggle pinned ↔ floating.
- Floating mode: rail icons reveal label tooltip after 400ms hover; whole rail + slid-out panel are a single mouse-enter region (closing only when leaving both).

### Theme toggle
- Topbar icon-button flips `data-theme="dark|light"` on `<html>`. Preference persisted.

### Toasts
- Sonner. Default 3.2s TTL. Used on: lesson saved, lesson deleted (with Undo action), prompt saved, repo added, API connection test, ticket re-review queued.

### Live behaviors (SSE)
The prototype uses local `setInterval` timers to fake SSE — replace with native `EventSource` (already in stack) wrapped in `useEventStream`.
- **Tickets list**: live ticket's token total + verdict-dot pulse update every ~700–1000ms.
- **Ticket detail · Review tab · running agent**: elapsed (1s), tokens-in (700ms), tokens-out (700ms), cost (1.5s), indeterminate bar (CSS-only).
- **Audit log**: new entries prepend with a 1.8s `flash-new` animation.
- **Dashboard**: most-recent activity row uses `flash-new`. Activity list and Live agents card update on SSE.
- **Topbar**: green `live` connection pill with a 2.6s soft pulse. When SSE drops show a banner under the topbar reading "Reconnecting…" — not in the prototype but reserved.

### Keyboard
- `/` focuses the search box on the Tickets list (prototype shows the hint chip).
- `⌘K` opens command palette (placeholder hook in prototype; build with shadcn's `<Command>`).

### Empty states
- Tickets list filtered to nothing: centered "No tickets match these filters" + "Clear filters" button.
- Memory in a repo with no lessons: hero CTA "Write the first lesson".
- Repos with none allowlisted: the add-form bar serves as the empty-state CTA.

---

## State vocabularies (badge + chip palette)

| Group | Values | Visual |
|---|---|---|
| Ticket status | `review`, `done` | accent badge / success badge |
| Kind | `new feature`, `bug fix`, `investigation` | neutral lowercase chip |
| PR state | `open`, `closed`, `merged` | small chip next to PR link |
| Review verdict | `APPROVED`, `CHANGES_REQUESTED`, `COMMENT` | success / danger / neutral badge |
| Review-job status | `queued`, `running`, `posted`, `failed`, `skipped`, `cancelled` | neutral / accent + pulse / verdict-colored / danger / soft-grey / soft-grey |
| Skip reason | `draft`, `fork`, `bot_author`, `trivial_diff`, `too_large`, `crashed`, `secrets_detected` | small chip below the verdict |
| Workspace state (admin) | `creating`, `active`, `expired`, `destroying`, `destroyed`, `destroy_failed` | last value gets a loud danger banner |
| Finding severity | `must-fix`, `nit`, `suggestion`, `info` | danger / warning / info / neutral 8px square |
| Actor kind | `github_user`, `agent`, `system` | round avatar (initials) / rounded violet square (initial) / rounded grey square (•) |

---

## Domain entities (drop-in for the API client)

Keys map directly to the prototype's mock data (`app/data.js`). The backend's OpenAPI schema is authoritative — use `openapi-typescript` + `openapi-fetch` per the original stack constraints.

```
Ticket            { id, number, repo_id, repo, title, status, kind, source,
                    actor, created, updated, verdicts: { arch, sec, style },
                    cost_usd, tokens_total, pr, skip_reason?, is_live? }
PR                { number, state, is_draft, author, head, base, additions,
                    deletions, files, html_url }
ReviewJob         { status, verdict, started, posted, step, step_label, progress,
                    heartbeat_age_s, tokens_in, tokens_out, cost_usd, prompt_hash,
                    lessons_applied: string[], duration_s, findings: Finding[] }
Finding           { id, file, line, severity, title, body, snippet?, rationale?,
                    applied_lesson?: string }
Lesson            { id, title, body, source_pr, created, applied_count }
Repo              { id, name, plugin, lang, status, lessons_count, last_review_age_ms? }
ReviewerAgent     { id, name, short, coding_agent, is_built_in, hue, prompt,
                    applied_to }
AuditEntry        { id, kind, actor, ts, payload }
Actor             { kind: 'system'|'agent'|'github_user', name?, login? }
```

---

## Animations & motion

Restrained, ops-tool feel. All durations and easings:

| Animation | Duration | Easing |
|---|---|---|
| Toast in | 220ms | cubic-bezier(.22,1,.36,1) |
| Modal in | 180ms | cubic-bezier(.22,1,.36,1) |
| Sheet in | 240ms | cubic-bezier(.22,1,.36,1) |
| Sidebar panel slide | 180ms | cubic-bezier(.22,1,.36,1) |
| Conn-pulse (live) | 2.6s | ease-out infinite |
| Pulse-ring on running dot | 1.6s | ease-out infinite |
| Indeterminate progress bar | 1.4s | ease-in-out infinite |
| Spinner | 0.9s | linear infinite |
| Flash-new on new audit/activity row | 1.8s | ease-out once |
| Chevron expand | 180ms | (CSS transition) |

Keep these — the brief explicitly calls for "medium intensity" motion that says alive without being arcade.

---

## Assets

- **Logo**: placeholder only. The prototype draws a 24×24 rounded-square with a gradient (accent → purple) and a single "Y" glyph in Geist Mono Bold. There is a `LOGO · PLACEHOLDER` mono caption under the wordmark to make it obvious. **Replace with real brand mark when available.**
- **Fonts**: Geist + Geist Mono via Google Fonts (`@import` in `app/yaaof.css`). Self-host or use the same CDN.
- **Icons**: lucide-react. The prototype's hand-rolled icons in `app/icons.jsx` are placeholder stand-ins — substitute lucide imports 1:1 (names listed in the Fidelity table).
- **No raster images.**

---

## Source files

- `yaaof.html` — entrypoint, loads React 18.3.1 + @babel/standalone, sets initial theme/density, mounts `App`.
- `app/yaaof.css` — all design tokens + base styles (light + dark + density). ~570 lines.
- `app/data.js` — mock data layer; mirrors entity shape above.
- `app/icons.jsx` — placeholder icons (replace with lucide-react).
- `app/helpers.jsx` — `useNow`, `useRouter`, `Link`, formatters (`relTime`, `durationMs`, `fmtCost`, `fmtTokens`), `Avatar`, `VerdictBadge`, `StatusBadge`, `KindChip`, `SourceIcon`, `VerdictDots`, `SevDot`, `ToastProvider`, `useToast`, `SourceLine`, `UseAgo`.
- `app/shell.jsx` — `Sidebar`, `Topbar`, `crumbsFor`, NAV array.
- `app/screens-tickets.jsx` — `ScreenTickets`, `FilterBar`, `TicketRow`, etc.
- `app/screens-ticket-detail.jsx` — `ScreenTicket`, `TicketDetailHeader`, `ReviewTab`, `AuditTab`, `AgentCard`, `FindingRow`, `CodeSnippet`, summarize/prettyJson helpers.
- `app/screens-dashboard.jsx` — `ScreenDashboard`, `DashOnboarding`, `DashPopulated`, `MetricTile`, `Sparkline`, `LiveTicketRow`, `ActivityRow`.
- `app/screens-other.jsx` — `ScreenMemory`, `ScreenPrompts`, `ScreenRepos`, `ScreenSettings`, `NewLessonModal`.
- `app/app.jsx` — top-level `App` + `YaaofTweaks` panel.
- `app/tweaks-panel.jsx` — Tweaks panel primitive (skip when porting — it's a prototype-only tool).

---

## Out of scope for M01 (do not implement)

Verbatim from the original brief:

- Login / sign-up / SSO / user management
- Multi-org / org switcher
- Coding agent UI (review-only in M01)
- Linear / Jira / Slack intake UI
- Ephemeral test environment provisioning UI
- Merge-gating / branch-protection UI
- Budget UI / cost caps
- Custom user-defined review agents
- Aggregated cross-agent verdict
- Notification routing (Slack / email)
- Mobile / tablet layouts
- Marketing-site chrome

The current design forward-compats the future vision in two specific places:
1. The `kind` chip in ticket headers — set up for `new feature | bug fix | investigation`. Today renders informationally only.
2. The `Status` axis is the structural one. Adding `Implementing` (or any future status) becomes a new section header in the Group-by-Status view; no new component required.

---

## Implementation notes for Claude Code

1. Start by porting `app/yaaof.css` → Tailwind `theme.extend` + a global `@layer base` block. Keep the oklch values; don't degrade to HSL.
2. Wire the `data-theme` / `data-density` attributes from a Zustand store backed by `localStorage`. Persist `sidebarPinned` the same way.
3. The sidebar pin/float interaction has subtle mouse-leave logic — keep panel + rail as one hover region. See `Sidebar` in `app/shell.jsx`.
4. The Findings row is the most state-rich component on the page; consider lifting `expandedFindingId` up to the route so deep-links can pre-expand a specific finding.
5. SSE — use the native `EventSource` and TanStack Query's `setQueryData` to merge incoming events into the cached entity lists. The prototype's interval-based fake is in `LiveCounter` / `LiveElapsed` / `LiveCost` etc — replicate the *cadence* (700ms tokens, 1s elapsed, 1.5s cost) so the feel transfers.
6. All the components in `app/` are decomposed at the granularity the brief implies — directly mappable to a feature-folder structure (`features/tickets/`, `features/memory/`, etc.) under shadcn's typical layout.
