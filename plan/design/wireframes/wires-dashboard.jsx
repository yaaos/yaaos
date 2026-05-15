// wires-dashboard.jsx — dashboard variants (onboarding + populated)

// ─── Onboarding A: checklist cards (3 stacked banners) ───────────────────
function DashOnboardA() {
  return (
    <WFShell active="dash" crumbs={[{ label: 'Dashboard', active: true }]}>
      <WFNote tag="A · CHECKLIST" title="First-run, three big tasks">
        Each setup step is a full-width banner card with the action on the right.
        Reads top-to-bottom like a checklist; tradeoff is vertical space.
      </WFNote>
      <div className="wf-page-h">
        <div>
          <h1 className="wf-h1">Welcome to yaaof</h1>
          <div className="wf-sub">Three steps to your first review.</div>
        </div>
        <span className="wf-chip">0 of 3 complete</span>
      </div>
      <div className="c g12 fl">
        {[
          { n: 1, h: 'Install the GitHub App', sub: 'Grant yaaof access to the repos you want reviewed.', cta: 'Install', state: 'todo' },
          { n: 2, h: 'Add your model API key', sub: 'Anthropic, OpenAI, or compatible.', cta: 'Add key', state: 'todo' },
          { n: 3, h: 'Add a repo to the allowlist', sub: 'yaaof opens a ticket each time a PR lands on an allowlisted repo.', cta: 'Add repo', state: 'todo' },
        ].map((s) => (
          <div key={s.n} className="wf-block r g16" style={{ padding: '14px 16px' }}>
            <div style={{ width: 28, height: 28, borderRadius: '50%', border: '1.5px solid var(--wf-stroke)', display: 'grid', placeItems: 'center', fontWeight: 600, flex: 'none' }}>{s.n}</div>
            <div className="fl c g4">
              <div className="wf-tx" style={{ fontSize: 13, fontWeight: 600 }}>{s.h}</div>
              <div className="wf-tx-3" style={{ fontSize: 11 }}>{s.sub}</div>
            </div>
            <span className="wf-chip">todo</span>
            <button className="wf-btn wf-btn-primary">{s.cta}</button>
          </div>
        ))}
        <div className="wf-blank c g8" style={{ marginTop: 4 }}>
          <div className="wf-sec-h">Then…</div>
          <div className="wf-tx-3" style={{ fontSize: 11 }}>Open a PR on an allowlisted repo. yaaof will create a ticket and the three review agents will start working.</div>
        </div>
      </div>
    </WFShell>
  );
}

// ─── Onboarding B: stepper + preview underneath ──────────────────────────
function DashOnboardB() {
  return (
    <WFShell active="dash" crumbs={[{ label: 'Dashboard', active: true }]}>
      <WFNote tag="B · STEPPER" title="Compact stepper, dashboard chrome preview below">
        Tighter; shows what the populated dashboard will look like (greyed). Risk:
        the preview can look like the page is broken.
      </WFNote>
      <div className="wf-page-h">
        <h1 className="wf-h1">Set up yaaof</h1>
        <span className="wf-chip">0 / 3</span>
      </div>
      <div className="wf-block r g12" style={{ padding: 14 }}>
        {['Install GitHub App', 'Add API key', 'Add a repo'].map((s, i) => (
          <React.Fragment key={i}>
            <div className="r g8 fl">
              <div style={{ width: 22, height: 22, borderRadius: '50%', border: '1.5px solid var(--wf-stroke)', display: 'grid', placeItems: 'center', fontSize: 11, fontWeight: 600, flex: 'none' }}>{i + 1}</div>
              <div className="c g4 fl">
                <div style={{ fontWeight: 600, fontSize: 12 }}>{s}</div>
                <div className="wf-tx-3" style={{ fontSize: 10 }}>{i === 0 ? 'Required' : i === 1 ? 'Anthropic or compatible' : 'owner/name'}</div>
              </div>
              <button className="wf-btn wf-btn-primary">Start</button>
            </div>
            {i < 2 && <div style={{ height: 1, width: 24, background: 'var(--wf-stroke-soft)' }} />}
          </React.Fragment>
        ))}
      </div>
      <div className="wf-sec-h" style={{ marginTop: 4 }}>Preview · once configured</div>
      <div className="fl c g12" style={{ opacity: 0.45 }}>
        <div className="r g12">
          {['Reviews 24h', 'Avg latency', 'Cost 24h', 'Open tickets'].map((m, i) => (
            <div key={i} className="wf-block fl c g8">
              <div className="wf-sec-h">{m}</div>
              <div className="wf-bar-strong" style={{ height: 18, width: '50%' }} />
              <div className="wf-bar-thin" style={{ width: '70%' }} />
            </div>
          ))}
        </div>
        <div className="wf-block fl c g8" style={{ minHeight: 0 }}>
          <div className="wf-sec-h">Activity</div>
          <WFLines count={5} widths={['85%', '95%', '70%', '90%', '60%']} />
        </div>
      </div>
    </WFShell>
  );
}

// ─── Populated A: classic — metrics row, agent overview, activity ───────
function DashPopA({ sidebarMode = 'pinned', noteOverride = null }) {
  return (
    <WFShell active="dash" crumbs={[{ label: 'Dashboard', active: true }]}
      sidebarMode={sidebarMode}
      topRight={<span className="wf-chip ok"><span className="dot" />live</span>}>
      {noteOverride || (
        <WFNote tag="A · METRICS-FIRST" title="Tiles row on top, two-col split below">
          Datadog-ish. Operator-friendly; engineers may skim past the metrics straight
          to the activity ticker.
        </WFNote>
      )}
      <div className="wf-page-h">
        <div>
          <h1 className="wf-h1">Overview</h1>
          <div className="wf-sub">acme · last 24h</div>
        </div>
        <div className="r g8">
          <span className="wf-chip">acme</span>
          <button className="wf-btn">Last 24h ▾</button>
        </div>
      </div>
      {/* Metrics row */}
      <div className="r g12">
        {[
          { l: 'Reviews 24h', v: '47', d: '+12', sub: 'spark' },
          { l: 'Avg latency', v: '3m 04s', d: '−8s' },
          { l: 'Cost 24h',    v: '$4.82', d: '+$0.41' },
          { l: 'Open tickets',v: '3', d: '' },
          { l: 'Queue · workers', v: '1 · 2 / 4', d: '' },
        ].map((m, i) => (
          <div key={i} className="wf-block fl c g8">
            <div className="r between">
              <div className="wf-sec-h">{m.l}</div>
              {m.d && <span className="wf-tx-3" style={{ fontSize: 10, fontFamily: 'JetBrains Mono, monospace' }}>{m.d}</span>}
            </div>
            <div style={{ fontSize: 22, fontWeight: 600, letterSpacing: '-0.02em', fontFamily: 'JetBrains Mono, monospace' }}>{m.v}</div>
            {m.sub === 'spark' ? (
              <svg viewBox="0 0 100 24" preserveAspectRatio="none" style={{ width: '100%', height: 22 }}>
                <polyline points="0,18 8,20 16,22 24,22 32,20 40,14 48,10 56,6 64,12 72,16 80,12 88,18 96,16" fill="none" stroke="var(--wf-stroke-2)" strokeWidth="1.2" />
              </svg>
            ) : <div className="wf-bar-thin" style={{ width: '40%' }} />}
          </div>
        ))}
      </div>
      {/* Lower split */}
      <div className="r g12 fl start" style={{ minHeight: 0 }}>
        <div className="wf-block c g10 fl" style={{ minHeight: 0 }}>
          <div className="r between">
            <div className="wf-sec-h">Live agents · in-flight</div>
            <span className="wf-tx-3" style={{ fontSize: 10 }}>3 ticket{'·'}3 jobs</span>
          </div>
          {[
            { t: '#2431 · acme/web · ticket-sse', a: ['running', 'queued', 'APPROVED'] },
            { t: '#2430 · acme/api · problem-nested', a: ['APPROVED', 'CHANGES_REQUESTED', 'APPROVED'] },
            { t: '#2429 · acme/web · audit-filters', a: ['APPROVED', 'APPROVED', 'COMMENT'] },
          ].map((t, i) => (
            <div key={i} className="c g6" style={{ padding: '8px 0', borderTop: i ? '1px solid var(--wf-stroke-soft)' : 0 }}>
              <div className="r between">
                <div className="wf-tx" style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11 }}>{t.t}</div>
                <span className="wf-tx-4" style={{ fontSize: 10 }}>updated 6s</span>
              </div>
              <div className="r g6">
                {t.a.map((v, j) => (
                  <React.Fragment key={j}>
                    <span className="wf-tx-3" style={{ fontSize: 9.5, fontFamily: 'JetBrains Mono, monospace', width: 36 }}>{['arch','sec','style'][j]}</span>
                    <VerdictChip verdict={v} />
                    {j < 2 && <div style={{ width: 8 }} />}
                  </React.Fragment>
                ))}
              </div>
            </div>
          ))}
        </div>
        <div className="wf-block c g8" style={{ width: 360, minHeight: 0 }}>
          <div className="wf-sec-h">Activity</div>
          {[
            'style approved · #2431 · 35s ago',
            'sec requested changes · #2430 · 2m ago',
            'style commented · #2429 · 4m ago',
            'PR opened · #2431 · rachel-cohen · 4m ago',
            'PR opened · #2430 · mark-i · 12m ago',
            'PR merged · #2428 · 90m ago',
            'lesson added · acme/web · 4h ago',
          ].map((a, i) => (
            <div key={i} className="r g8" style={{ fontSize: 11, color: 'var(--wf-text-2)', padding: '4px 0', borderTop: i ? '1px solid var(--wf-stroke-soft)' : 0 }}>
              <span className="wf-av sys" style={{ width: 14, height: 14 }} />
              <span className="fl">{a}</span>
            </div>
          ))}
        </div>
      </div>
    </WFShell>
  );
}

// ─── Populated B: activity-first ─────────────────────────────────────────
function DashPopB() {
  return (
    <WFShell active="dash" crumbs={[{ label: 'Dashboard', active: true }]}
      topRight={<span className="wf-chip ok"><span className="dot" />live</span>}>
      <WFNote tag="B · ACTIVITY-FIRST" title="One big stream, metrics rail on right">
        Mirrors the engineer mental model (what just happened?). Less authoritative
        for operators monitoring health.
      </WFNote>
      <div className="wf-page-h">
        <h1 className="wf-h1">Activity</h1>
        <div className="r g6">
          {['All', 'PRs', 'Reviews', 'Lessons', 'System'].map((f, i) => (
            <span key={i} className={"wf-chip" + (i === 0 ? ' acc' : '')}>{f}</span>
          ))}
        </div>
      </div>
      <div className="r g12 fl start" style={{ minHeight: 0 }}>
        <div className="wf-box fl c" style={{ minHeight: 0, overflow: 'hidden' }}>
          {[
            { t: 'style approved', d: '#2431 acme/web · 2 findings · $0.14', age: '35s' },
            { t: 'arch invoking agent', d: '#2431 acme/web · t+90s', age: '1m', live: true },
            { t: 'sec requested changes', d: '#2430 acme/api · 1 must-fix · $0.24', age: '2m', bad: true },
            { t: 'style commented', d: '#2429 acme/web · 0 findings · $0.06', age: '4m' },
            { t: 'PR opened', d: '#2431 by rachel-cohen on acme/web', age: '4m' },
            { t: 'PR opened', d: '#2430 by mark-i on acme/api', age: '12m' },
            { t: 'PR opened', d: '#2429 by priya-shah on acme/web', age: '22m' },
            { t: 'PR merged', d: '#2428 acme/api · deps: psycopg-3.2.3', age: '90m' },
            { t: 'lesson added', d: '"Don\'t suggest mocks" — acme/web', age: '4h' },
          ].map((a, i) => (
            <div key={i} className="r g10" style={{ padding: '10px 14px', borderTop: i ? '1px solid var(--wf-stroke-soft)' : 0 }}>
              <span className={"wf-av " + (a.live ? 'agent' : 'sys')} />
              <div className="c g4 fl">
                <div className="r g6">
                  <span className="wf-tx" style={{ fontSize: 12, fontWeight: 600 }}>{a.t}</span>
                  {a.live && <span className="wf-pulse" style={{ width: 6, height: 6 }} />}
                  {a.bad && <span className="wf-chip bad"><span className="dot" />must-fix</span>}
                </div>
                <div className="wf-tx-3" style={{ fontSize: 10.5, fontFamily: 'JetBrains Mono, monospace' }}>{a.d}</div>
              </div>
              <span className="wf-tx-4" style={{ fontSize: 10 }}>{a.age}</span>
            </div>
          ))}
        </div>
        <div className="c g12" style={{ width: 240, flex: 'none' }}>
          {[
            { l: 'Reviews 24h', v: '47', d: '+12' },
            { l: 'Avg latency', v: '3m 04s', d: '−8s' },
            { l: 'Cost 24h',    v: '$4.82', d: '+$0.41' },
            { l: 'Open',        v: '3', d: '' },
            { l: 'Queue · wkrs',v: '1 · 2/4', d: '' },
          ].map((m, i) => (
            <div key={i} className="wf-block c g6" style={{ padding: 10 }}>
              <div className="r between">
                <div className="wf-sec-h">{m.l}</div>
                {m.d && <span className="wf-tx-3" style={{ fontSize: 10, fontFamily: 'JetBrains Mono, monospace' }}>{m.d}</span>}
              </div>
              <div style={{ fontSize: 18, fontWeight: 600, fontFamily: 'JetBrains Mono, monospace' }}>{m.v}</div>
            </div>
          ))}
        </div>
      </div>
    </WFShell>
  );
}

// ─── Populated C: split — open tickets card primary ──────────────────────
function DashPopC() {
  return (
    <WFShell active="dash" crumbs={[{ label: 'Dashboard', active: true }]}
      topRight={<span className="wf-chip ok"><span className="dot" />live</span>}>
      <WFNote tag="C · TICKETS-FIRST" title="Open tickets are the hero; metrics rail on top compact">
        Push 'what's in flight' to the front. Strongest for engineers landing here
        with a PR open; weaker for ops monitoring.
      </WFNote>
      {/* Compact metric strip */}
      <div className="wf-box r" style={{ overflow: 'hidden' }}>
        {[
          { l: '47', s: 'reviews/24h', d: '+12' },
          { l: '3m 04s', s: 'avg latency', d: '−8s' },
          { l: '$4.82', s: 'cost/24h', d: '+$0.41' },
          { l: '3', s: 'open', d: '' },
          { l: '2 / 4', s: 'workers', d: '' },
          { l: '2.1%', s: 'fail rate', d: '' },
        ].map((m, i) => (
          <div key={i} className="r between fl" style={{ padding: '12px 16px', borderLeft: i ? '1px solid var(--wf-stroke-soft)' : 0, minWidth: 0 }}>
            <div className="c g4 fl">
              <div style={{ fontSize: 17, fontWeight: 600, fontFamily: 'JetBrains Mono, monospace' }}>{m.l}</div>
              <div className="wf-tx-3" style={{ fontSize: 10 }}>{m.s}</div>
            </div>
            {m.d && <span className="wf-tx-3" style={{ fontSize: 10, fontFamily: 'JetBrains Mono, monospace' }}>{m.d}</span>}
          </div>
        ))}
      </div>
      <div className="r between">
        <div className="wf-sec-h">In flight · 3</div>
        <span className="wf-tx-3" style={{ fontSize: 10 }}>Updates live · last change 6s ago</span>
      </div>
      <div className="c g10 fl" style={{ minHeight: 0, overflow: 'hidden' }}>
        {[
          { id: '#2431', repo: 'acme/web', title: 'Add real-time SSE handling to ticket detail', by: 'rachel-cohen', v: ['running', 'queued', 'APPROVED'], age: '4m' },
          { id: '#2430', repo: 'acme/api', title: 'Refactor problem-details middleware', by: 'mark-i', v: ['APPROVED', 'CHANGES_REQUESTED', 'APPROVED'], age: '12m' },
          { id: '#2429', repo: 'acme/web', title: 'Wire up audit-log filter chips to URL state', by: 'priya-shah', v: ['APPROVED', 'APPROVED', 'COMMENT'], age: '22m' },
        ].map((t, i) => (
          <div key={i} className="wf-block c g10" style={{ padding: 14 }}>
            <div className="r between">
              <div className="r g8">
                <span className="wf-tx-mono wf-tx-3" style={{ fontSize: 11 }}>{t.id}</span>
                <span className="wf-tx-mono wf-tx-3" style={{ fontSize: 11 }}>{t.repo}</span>
                <span className="wf-tx" style={{ fontWeight: 600 }}>{t.title}</span>
              </div>
              <div className="r g8">
                <span className="wf-tx-4" style={{ fontSize: 10 }}>{t.age}</span>
                <span className="wf-av" /> <span className="wf-tx-3" style={{ fontSize: 10.5 }}>{t.by}</span>
              </div>
            </div>
            <div className="r g16">
              {t.v.map((v, j) => (
                <div key={j} className="r g6 fl">
                  <span className="wf-tx-mono wf-tx-3" style={{ fontSize: 10, width: 40 }}>{['arch','sec','style'][j]}</span>
                  <VerdictChip verdict={v} />
                  {v === 'running' && <div className="wf-progress indet fl"><div /></div>}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </WFShell>
  );
}

Object.assign(window, {
  DashOnboardA, DashOnboardB,
  DashPopA, DashPopB, DashPopC,
  PopPinned, PopFloatClosed, PopFloat,
});

// ─── Sidebar mode comparison wrappers ─────────────────────────────────
function ModeNote({ mode }) {
  if (mode === 'pinned') return (
    <WFNote tag="STATE · PINNED" title="Pin button in footer is active (📌). Sidebar takes its own column (~180px).">
      <b>User toggles state via the pin button</b> in the sidebar footer. Pinned
      is the default after first run. Unpin → switches to floating + collapses
      the panel to a 44px rail.
    </WFNote>
  );
  if (mode === 'floating-closed') return (
    <WFNote tag="STATE · FLOATING, IDLE" title="Pin off · sidebar collapsed to icon rail">
      Maximum content width. The rail stays visible so nav is one click away. Pin
      button moves to the rail footer (⌘); clicking it re-pins. Hover/click the
      rail → panel slides out (next artboard).
    </WFNote>
  );
  return (
    <WFNote tag="STATE · FLOATING, OPEN" title="Pin off · panel revealed by hover or click">
      The full panel overlays content while open; closes when pointer leaves or
      after navigation. Pin button at the bottom of the panel re-pins it.
    </WFNote>
  );
}

function PopPinned() { return <DashPopA sidebarMode="pinned"   noteOverride={<ModeNote mode="pinned"   />} />; }
function PopFloatClosed() { return <DashPopA sidebarMode="floating" sidebarPanelOpen={false} noteOverride={<ModeNote mode="floating-closed" />} />; }
function PopFloat()  { return <DashPopA sidebarMode="floating" sidebarPanelOpen={true} noteOverride={<ModeNote mode="floating" />} />; }
