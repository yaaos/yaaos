// wires-shell.jsx — reusable shell + sidebar + topbar primitives

const WF_NAV = [
  { id: 'dash', label: 'Dashboard', count: null },
  { id: 'tix',  label: 'Tickets',   count: '3' },
  { id: 'mem',  label: 'Memory',    count: null },
  { id: 'pmt',  label: 'Prompts',   count: null },
  { id: 'rep',  label: 'Repos',     count: '4' },
  { id: 'set',  label: 'Settings',  count: null },
];

function WFLogo() {
  return (
    <div className="r g8">
      <div className="wf-logo"><span>Y</span></div>
      <div className="wf-wordmark">yaaof<small>LOGO PLACEHOLDER</small></div>
    </div>
  );
}

// Sidebar has one logical primitive with two user-controlled states:
//   - pinned:   full panel takes its column in the grid (~180px)
//   - floating: collapsed to a 44px rail; panel overlays content on hover/click
// User toggles between them with a pin button inside the sidebar (📌 / ⌽).
// The wireframe shows two artboards: one pinned, one floating-with-panel-out,
// so the affordance + the two states are visible at a glance.
function WFSidebar({ active = 'dash', mode = 'pinned', panelOpen = false }) {
  const isFloat = mode === 'floating';
  return (
    <aside className={"wf-sb " + (isFloat ? 'wf-sb-rail wf-sb-float' : '')}>
      {isFloat ? (
        <>
          <div className="wf-sb-brand wf-sb-brand-rail">
            <div className="wf-logo" title="yaaof"><span>Y</span></div>
          </div>
          <div className="wf-sb-nav wf-sb-nav-rail">
            {WF_NAV.map((n) => (
              <div key={n.id} className={"wf-sb-item wf-sb-item-rail " + (active === n.id ? 'act' : '')} title={n.label}>
                <span className="ico" />
                {n.count && <span className="ct-dot" />}
              </div>
            ))}
          </div>
          <div className="wf-sb-foot-rail" title="floating">
            <div className="wf-pin-btn" title="Pin sidebar">⌘</div>
          </div>
          {panelOpen && (
            <div className="wf-sb-float-panel">
              <div className="wf-sb-brand"><WFLogo /></div>
              <div className="wf-sb-nav">
                <div className="wf-sb-sec">Workspace</div>
                {WF_NAV.map((n) => (
                  <div key={n.id} className={"wf-sb-item " + (active === n.id ? 'act' : '')}>
                    <span className="ico" />
                    <span>{n.label}</span>
                    {n.count && <span className="ct">{n.count}</span>}
                  </div>
                ))}
              </div>
              <div className="wf-sb-foot">
                <button className="wf-btn wf-btn-ghost" style={{ height: 22, fontSize: 10.5, padding: '0 6px', marginLeft: 'auto', gap: 4 }} title="Pin sidebar">
                  <span style={{ fontSize: 11 }}>📌</span> Pin
                </button>
              </div>
            </div>
          )}
        </>
      ) : (
        <>
          <div className="wf-sb-brand"><WFLogo /></div>
          <div className="wf-sb-nav">
            <div className="wf-sb-sec">Workspace</div>
            {WF_NAV.map((n) => (
              <div key={n.id} className={"wf-sb-item " + (active === n.id ? 'act' : '')}>
                <span className="ico" />
                <span>{n.label}</span>
                {n.count && <span className="ct">{n.count}</span>}
              </div>
            ))}
          </div>
          <div className="wf-sb-foot">
            <span style={{ width: 6, height: 6, borderRadius: 999, background: 'var(--wf-success)', border: '1px solid #1d1f24' }} />
            <span>v0.4.2</span>
            <span className="fl" />
            <button className="wf-btn wf-btn-ghost wf-pin-btn-pinned" style={{ height: 22, fontSize: 10.5, padding: '0 6px', gap: 4 }} title="Unpin sidebar">
              <span style={{ fontSize: 11 }}>📌</span>
            </button>
          </div>
        </>
      )}
    </aside>
  );
}

function WFTop({ crumbs = [], right = null, sidebarMode = 'pinned' }) {
  return (
    <header className="wf-top">
      {(sidebarMode === 'rail' || sidebarMode === 'floating') && (
        <button className="wf-btn wf-btn-ghost wf-btn-icon" title="Expand sidebar" style={{ width: 22, height: 22, padding: 0, marginLeft: -4 }}>
          <span style={{ display: 'inline-block', width: 12, height: 9, position: 'relative' }}>
            <span style={{ position: 'absolute', left: 0, right: 0, top: 0, height: 1.5, background: 'var(--wf-text-2)' }} />
            <span style={{ position: 'absolute', left: 0, right: 0, top: 3.5, height: 1.5, background: 'var(--wf-text-2)' }} />
            <span style={{ position: 'absolute', left: 0, right: 0, top: 7, height: 1.5, background: 'var(--wf-text-2)' }} />
          </span>
        </button>
      )}
      {crumbs.map((c, i) => (
        <React.Fragment key={i}>
          <span className="wf-crumb">{c.active ? <b>{c.label}</b> : c.label}</span>
          {i < crumbs.length - 1 && <span className="wf-crumb sep">/</span>}
        </React.Fragment>
      ))}
      <div className="fl" />
      {right}
      <div className="wf-search">
        <span className="icon" /> <span>Command…</span>
        <span style={{ marginLeft: 'auto', fontFamily: 'JetBrains Mono, monospace', fontSize: 10, color: 'var(--wf-text-4)' }}>⌘K</span>
      </div>
    </header>
  );
}

function WFShell({ active, crumbs, topRight, children, noSidebar = false, sidebarMode = 'pinned', sidebarPanelOpen = false }) {
  const cls = "wf-app"
    + (noSidebar ? ' no-sidebar' : '')
    + (sidebarMode === 'floating' ? ' wf-app-float' : '');
  return (
    <div className={cls}>
      {!noSidebar && <WFSidebar active={active} mode={sidebarMode} panelOpen={sidebarPanelOpen} />}
      <main className="wf-main">
        <WFTop crumbs={crumbs} right={topRight} sidebarMode={sidebarMode} />
        <div className="wf-page">{children}</div>
      </main>
    </div>
  );
}

function WFNote({ title, children, tag = 'NOTE' }) {
  return (
    <div className="wf-note">
      <span className="tag">{tag}</span>
      <div><b>{title}</b>{children ? ' — ' : ''}{children}</div>
    </div>
  );
}

function WFBlock({ children, style }) {
  return <div className="wf-block" style={style}>{children}</div>;
}

// Greybox text lines
function WFLines({ count = 3, widths = ['100%', '80%', '60%'] }) {
  return (
    <div className="c g6">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="wf-bar" style={{ width: widths[i % widths.length], height: 6 }} />
      ))}
    </div>
  );
}

// Verdict chip helper
function VerdictChip({ verdict }) {
  if (verdict === 'APPROVED')         return <span className="wf-chip solid-ok"><span className="dot" style={{ background: 'white' }} />APPROVED</span>;
  if (verdict === 'CHANGES_REQUESTED')return <span className="wf-chip solid-bad"><span className="dot" style={{ background: 'white' }} />CHANGES</span>;
  if (verdict === 'COMMENT')          return <span className="wf-chip solid-neut"><span className="dot" style={{ background: 'white' }} />COMMENT</span>;
  if (verdict === 'running')          return <span className="wf-chip acc"><span className="dot" />running</span>;
  if (verdict === 'queued')           return <span className="wf-chip"><span className="dot" />queued</span>;
  if (verdict === 'skipped')          return <span className="wf-chip" style={{ color: 'var(--wf-text-4)' }}><span className="dot" />skipped</span>;
  return <span className="wf-chip">{verdict}</span>;
}

// Status chip for ticket state
// M01: only `review` and `done`. Future statuses (e.g. `implementing`) plug into
// the same primitive — just add a new tone.
function StatusChip({ status }) {
  if (status === 'review') return <span className="wf-chip acc"><span className="dot" />Review</span>;
  if (status === 'done')   return <span className="wf-chip ok"><span className="dot" />Done</span>;
  // legacy values kept so older mocks don't break
  if (status === 'in_review') return <span className="wf-chip acc"><span className="dot" />Review</span>;
  if (status === 'complete')  return <span className="wf-chip ok"><span className="dot" />Done</span>;
  if (status === 'open')      return <span className="wf-chip acc"><span className="dot" />Review</span>;
  if (status === 'abandoned') return <span className="wf-chip ok"><span className="dot" />Done</span>;
  return <span className="wf-chip">{status}</span>;
}

// Kind chip — informational only, sits next to status in headers / rows
function KindChip({ kind = 'new feature' }) {
  return (
    <span className="wf-chip" style={{ fontFamily: 'Inter, sans-serif', textTransform: 'lowercase' }}>
      <span className="dot" style={{ background: 'var(--wf-text-3)' }} />{kind}
    </span>
  );
}

// Source icon — generalizes future origins (PR, Linear, Slack, ops alert).
// Today only `github_pr` exists; the shape is generic so adding `linear`
// later is a one-line change.
function SourceIcon({ source = 'github_pr' }) {
  return (
    <span
      title={source}
      className="wf-tx-3"
      style={{
        display: 'inline-grid', placeItems: 'center',
        width: 16, height: 16,
        border: '1px solid var(--wf-stroke-2)', borderRadius: 3,
        fontFamily: 'JetBrains Mono, monospace', fontSize: 9, fontWeight: 700, flex: 'none',
      }}
    >
      {source === 'github_pr' ? 'PR' : source === 'linear' ? 'L' : source === 'slack' ? 'S' : source === 'ops_alert' ? '!' : '?'}
    </span>
  );
}

// Origin/source row used in ticket-detail headers. Reads as a sentence so it
// generalises beyond GitHub PRs (next: Linear, Slack, ops alerts).
function SourceLine({
  source = 'github_pr', refId = '#2431', repo = 'acme/web', actor = 'rachel-cohen',
  verb = 'opened', age = '4m ago', meta = 'feat/ticket-sse → main · +247 −38 in 11 files',
}) {
  return (
    <div className="r g8 wf-tx-3" style={{ fontSize: 11, flexWrap: 'wrap' }}>
      <span className="wf-tx-3" style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600 }}>Source</span>
      <SourceIcon source={source} />
      <span className="wf-tx-2"><b className="wf-tx wf-tx-mono">{source === 'github_pr' ? 'PR' : source} {refId}</b> on <b className="wf-tx wf-tx-mono">{repo}</b></span>
      <span>·</span>
      <span className="r g4"><span className="wf-av" style={{ width: 12, height: 12 }} />{actor}</span>
      <span>·</span>
      <span>{verb} {age}</span>
      <span>·</span>
      <span className="wf-tx-mono">{meta}</span>
      <span>·</span>
      <span className="wf-link">open in github ↗</span>
    </div>
  );
}

Object.assign(window, {
  WFLogo, WFSidebar, WFTop, WFShell, WFNote, WFBlock, WFLines,
  VerdictChip, StatusChip, KindChip, SourceIcon, SourceLine, WF_NAV,
});
