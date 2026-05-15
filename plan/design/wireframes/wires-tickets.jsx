// wires-tickets.jsx — tickets list (updated)
//
// One layout direction (dense table). The same primitive renders two modes:
//   - flat: all rows in newest-first order; status is a column
//   - grouped: rows partitioned by Status with section headers
//
// Toggle lives in the top-right of the page header. Today: Review / Done. Add
// a new status later (e.g. Implementing) and it becomes another section header
// in grouped mode with no shell changes.

const TIX_ROWS = [
  { id: '#2431', repo: 'acme/web',    title: 'Add real-time SSE handling to ticket detail',           by: 'rachel-cohen', status: 'review', kind: 'new feature', source: 'github_pr', v: ['running','queued','APPROVED'],            age: '4m',  cost: '$0.32' },
  { id: '#2430', repo: 'acme/api',    title: 'Refactor problem-details middleware to handle nested errors', by: 'mark-i',  status: 'review', kind: 'bug fix',     source: 'github_pr', v: ['APPROVED','CHANGES_REQUESTED','APPROVED'], age: '12m', cost: '$0.51' },
  { id: '#2429', repo: 'acme/web',    title: 'Wire up audit-log filter chips to URL state',           by: 'priya-shah',    status: 'review', kind: 'new feature', source: 'github_pr', v: ['APPROVED','APPROVED','COMMENT'],          age: '22m', cost: '$0.27' },
  { id: '#2428', repo: 'acme/api',    title: 'Bump psycopg to 3.2.3 + fix connection pool sizing',    by: 'dev-bot',       status: 'done',   kind: 'bug fix',     source: 'github_pr', v: ['APPROVED','APPROVED','APPROVED'],         age: '2h',  cost: '$0.18' },
  { id: '#2427', repo: 'acme/mobile', title: 'Onboarding flow — step 3 should preserve email',        by: 'sam-ng',        status: 'review', kind: 'bug fix',     source: 'github_pr', v: ['skipped','skipped','skipped'],            age: '3h',  cost: '—',     skip: 'draft' },
  { id: '#2426', repo: 'acme/web',    title: 'Lessons modal: tab-trap when textarea has focus',       by: 'rachel-cohen',  status: 'done',   kind: 'bug fix',     source: 'github_pr', v: ['APPROVED','APPROVED','COMMENT'],          age: '5h',  cost: '$0.22' },
  { id: '#2425', repo: 'acme/api',    title: 'Worker: retry-on-429 with exponential backoff',         by: 'mark-i',        status: 'done',   kind: 'new feature', source: 'github_pr', v: ['APPROVED','COMMENT','APPROVED'],          age: '7h',  cost: '$0.38' },
  { id: '#2424', repo: 'acme/mobile', title: 'iOS 17 deprecation warnings in PushKit handler',        by: 'sam-ng',        status: 'done',   kind: 'bug fix',     source: 'github_pr', v: ['APPROVED','APPROVED','CHANGES_REQUESTED'],age: '9h',  cost: '$0.14' },
  { id: '#2423', repo: 'acme/web',    title: 'Use TanStack Query for repo allowlist hooks',           by: 'priya-shah',    status: 'done',   kind: 'new feature', source: 'github_pr', v: ['APPROVED','APPROVED','APPROVED'],         age: '1d',  cost: '$0.41' },
  { id: '#2422', repo: 'acme/api',    title: 'Fix N+1 in /tickets list endpoint',                     by: 'mark-i',        status: 'done',   kind: 'bug fix',     source: 'github_pr', v: ['COMMENT','APPROVED','APPROVED'],          age: '1d',  cost: '$0.29' },
];

function FilterChips({ groupBy = 'none' }) {
  return (
    <div className="r between">
      <div className="r g6">
        <span className="wf-chip acc"><span className="dot" />Review · 4</span>
        <span className="wf-chip"><span className="dot" />Done · 6</span>
        <span style={{ width: 8 }} />
        <span className="wf-chip">repo: all ▾</span>
        <span className="wf-chip">kind: all ▾</span>
        <span className="wf-chip">verdict: any ▾</span>
        <span className="wf-chip">author: any ▾</span>
      </div>
      <div className="r g6">
        <span className="wf-tx-3" style={{ fontSize: 10 }}>group by</span>
        <button className={"wf-btn" + (groupBy === 'none' ? ' wf-btn-primary' : '')} style={{ fontSize: 10.5, height: 22 }}>None</button>
        <button className={"wf-btn" + (groupBy === 'status' ? ' wf-btn-primary' : '')} style={{ fontSize: 10.5, height: 22 }}>Status</button>
      </div>
    </div>
  );
}

// grid: status, #, repo, title, kind, agents (3), source, author, age, cost
const COLS = '80px 60px 110px 1fr 100px 150px 32px 110px 50px 56px';

function VerdictDots({ v }) {
  return (
    <div className="r g4">
      {v.map((vv, j) => (
        vv === 'APPROVED' ? <span key={j} className="wf-sev" style={{ background: 'var(--wf-success)' }} />
        : vv === 'CHANGES_REQUESTED' ? <span key={j} className="wf-sev must" />
        : vv === 'COMMENT' ? <span key={j} className="wf-sev" style={{ background: 'var(--wf-text-3)' }} />
        : vv === 'running' ? <span key={j} className="wf-pulse" style={{ width: 10, height: 10 }} />
        : vv === 'queued' ? <span key={j} className="wf-sev" style={{ background: 'var(--wf-fill-4)', border: '1px solid var(--wf-stroke-2)' }} />
        : <span key={j} className="wf-sev" style={{ background: 'transparent', border: '1px dashed var(--wf-stroke-3)' }} />
      ))}
      <span className="wf-tx-4" style={{ fontSize: 9.5, marginLeft: 4 }}>arch sec sty</span>
    </div>
  );
}

function TicketRow({ t }) {
  return (
    <div className="wf-trow" style={{ gridTemplateColumns: COLS }}>
      <div><StatusChip status={t.status} /></div>
      <div className="wf-tx-mono wf-tx-3" style={{ fontSize: 11 }}>{t.id}</div>
      <div className="wf-tx-mono" style={{ fontSize: 11 }}>{t.repo}</div>
      <div className="wf-tx" style={{ fontSize: 12, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.title}</div>
      <div><KindChip kind={t.kind} /></div>
      <div><VerdictDots v={t.v} /></div>
      <div><SourceIcon source={t.source} /></div>
      <div className="r g6"><span className="wf-av" /><span className="wf-tx-3" style={{ fontSize: 11 }}>{t.by}</span></div>
      <div className="wf-tx-4 wf-tx-mono" style={{ fontSize: 10.5 }}>{t.age}</div>
      <div className="wf-tx-3 wf-tx-mono" style={{ fontSize: 10.5 }}>{t.cost}</div>
    </div>
  );
}

// ─── Flat mode (default) ───────────────────────────────────────────────
function TicketsA() {
  return (
    <WFShell active="tix" crumbs={[{ label: 'Tickets', active: true }]}>
      <WFNote tag="TICKETS · DEFAULT" title="Dense table, flat newest-first">
        Status is a column. <b>Kind</b> chip rides next to the title (informational
        only). <b>Source</b> column generalises beyond GitHub PRs. Add a new status
        later → it just becomes another value in the Status column / a new section
        in grouped mode.
      </WFNote>
      <div className="wf-page-h">
        <h1 className="wf-h1">Tickets</h1>
        <div className="r g8">
          <div className="wf-search"><span className="icon" /><span>Filter tickets…</span></div>
          <button className="wf-btn">Sort: newest ▾</button>
        </div>
      </div>
      <FilterChips groupBy="none" />
      <div className="wf-box fl c" style={{ minHeight: 0, overflow: 'hidden' }}>
        <div className="wf-thead" style={{ gridTemplateColumns: COLS }}>
          <div>status</div><div>#</div><div>repo</div><div>title</div><div>kind</div><div>review</div><div>src</div><div>author</div><div>age</div><div>cost</div>
        </div>
        <div className="c" style={{ overflow: 'hidden' }}>
          {TIX_ROWS.map((t, i) => <TicketRow key={i} t={t} />)}
        </div>
      </div>
    </WFShell>
  );
}

// ─── Grouped-by-status mode (toggled) ──────────────────────────────────
function TicketsAGrouped() {
  const groups = [
    { name: 'Review', tone: 'acc', items: TIX_ROWS.filter((t) => t.status === 'review') },
    { name: 'Done',   tone: 'ok',  items: TIX_ROWS.filter((t) => t.status === 'done') },
  ];
  // Same row primitive — just drop the leading status column.
  const cols = '60px 110px 1fr 100px 150px 32px 110px 50px 56px';
  return (
    <WFShell active="tix" crumbs={[{ label: 'Tickets', active: true }]}>
      <WFNote tag="TICKETS · GROUPED BY STATUS" title="Same data, toggled view — Status row partitions the list">
        Same primitive renders both views. Status column hides (it's the section
        header now). When <b>Implementing</b> arrives, it just becomes a new
        section header above Review — no new component, no new layout.
      </WFNote>
      <div className="wf-page-h">
        <h1 className="wf-h1">Tickets</h1>
        <div className="r g8">
          <div className="wf-search"><span className="icon" /><span>Filter tickets…</span></div>
          <button className="wf-btn">Sort: newest ▾</button>
        </div>
      </div>
      <FilterChips groupBy="status" />
      <div className="c g12 fl" style={{ minHeight: 0, overflow: 'hidden' }}>
        {groups.map((g) => (
          <div key={g.name} className="c g6">
            <div className="r g8 baseline">
              <StatusChip status={g.name.toLowerCase()} />
              <span className="wf-tx-3 wf-tx-mono" style={{ fontSize: 11 }}>{g.items.length}</span>
              <div className="fl" />
              {g.name === 'Review' && <span className="wf-tx-3" style={{ fontSize: 10 }}>updates live · last change 6s ago</span>}
            </div>
            <div className="wf-box c" style={{ overflow: 'hidden' }}>
              <div className="wf-thead" style={{ gridTemplateColumns: cols }}>
                <div>#</div><div>repo</div><div>title</div><div>kind</div><div>review</div><div>src</div><div>author</div><div>age</div><div>cost</div>
              </div>
              {g.items.map((t, i) => (
                <div key={i} className="wf-trow" style={{ gridTemplateColumns: cols }}>
                  <div className="wf-tx-mono wf-tx-3" style={{ fontSize: 11 }}>{t.id}</div>
                  <div className="wf-tx-mono" style={{ fontSize: 11 }}>{t.repo}</div>
                  <div className="wf-tx" style={{ fontSize: 12, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.title}</div>
                  <div><KindChip kind={t.kind} /></div>
                  <div><VerdictDots v={t.v} /></div>
                  <div><SourceIcon source={t.source} /></div>
                  <div className="r g6"><span className="wf-av" /><span className="wf-tx-3" style={{ fontSize: 11 }}>{t.by}</span></div>
                  <div className="wf-tx-4 wf-tx-mono" style={{ fontSize: 10.5 }}>{t.age}</div>
                  <div className="wf-tx-3 wf-tx-mono" style={{ fontSize: 10.5 }}>{t.cost}</div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </WFShell>
  );
}

// Legacy variants kept around (hidden in canvas state) but no longer needed.
function TicketsB() { return <TicketsA />; }
function TicketsC() { return <TicketsAGrouped />; }

Object.assign(window, { TicketsA, TicketsAGrouped, TicketsB, TicketsC });
