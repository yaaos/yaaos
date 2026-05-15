// wires-detail-audit.jsx — ticket detail: Audit log tab variants

const AUDIT_ENTRIES = [
  { k: 'review_job.posted',       a: 'style', tone: 'ok',  ts: '−35s',  summary: 'verdict APPROVED · 2 findings · 184s · $0.14' },
  { k: 'review_job.step_changed', a: 'system',tone: '',    ts: '−45s',  summary: 'style: posting → posted' },
  { k: 'review_job.step_changed', a: 'arch',  tone: 'acc', ts: '−50s',  summary: 'arch: awaiting_agent_output → invoking_agent · heartbeat 2s' },
  { k: 'review_job.heartbeat',    a: 'system',tone: '',    ts: '−60s',  summary: 'arch heartbeat 12s · ok' },
  { k: 'review_job.prompt_sent',  a: 'arch',  tone: 'acc', ts: '−90s',  summary: 'prompt p_arch_8c1a · 14.8k tokens · 4 lessons applied' },
  { k: 'review_job.started',      a: 'system',tone: '',    ts: '−92s',  summary: 'arch · worker w-3 · queue wait 1.84s' },
  { k: 'review_job.prompt_sent',  a: 'style', tone: 'acc', ts: '−220s', summary: 'prompt p_style_4f02 · 11.8k tokens · 4 lessons applied' },
  { k: 'lessons.read',            a: 'system',tone: '',    ts: '−222s', summary: 'acme/web · 4 lessons (l1, l2, l3, l4)' },
  { k: 'review_job.scheduled',    a: 'system',tone: '',    ts: '−4m',   summary: '3 agents queued · reason pull_request.opened · head 8c1a9f3' },
  { k: 'ticket.created',          a: 'user',  tone: 'acc', ts: '−4m',   summary: 'rachel-cohen · PR #2431 · acme/web' },
];

function FilterRow() {
  return (
    <div className="r g6">
      <span className="wf-chip acc">all · 10</span>
      <span className="wf-chip">review_job (8)</span>
      <span className="wf-chip">lessons (1)</span>
      <span className="wf-chip">ticket (1)</span>
      <span style={{ width: 8 }} />
      <span className="wf-chip">agent: any ▾</span>
      <span className="wf-chip">verdict: any ▾</span>
    </div>
  );
}

// Reuse TicketHeader by re-rendering it (defined in wires-detail-agents.jsx).
// It's on window via that file.

// ─── A: Hybrid rail (recommended) ──────────────────────────────────────
function AuditA() {
  return (
    <WFShell active="tix" crumbs={[{ label: 'Tickets' }, { label: '#2431', active: true }]}>
      <WFNote tag="A · RAIL + ROWS" title="Vertical timeline rail on left, dense mono rows on right">
        Flight-recorder feel; events read top-down, payloads are one click away.
        Recommended baseline.
      </WFNote>
      <TicketHeader active="audit" />
      <FilterRow />
      <div className="wf-box fl c" style={{ minHeight: 0, overflow: 'hidden' }}>
        <div className="r" style={{ borderBottom: '1px solid var(--wf-stroke)', background: 'var(--wf-fill-2)' }}>
          <div style={{ width: 60 }} />
          <div className="wf-thead" style={{ gridTemplateColumns: '160px 110px 1fr 60px', flex: 1, padding: '8px 0 8px 12px', background: 'transparent', border: 0 }}>
            <div>kind</div><div>actor</div><div>summary</div><div>when</div>
          </div>
        </div>
        <div className="r fl" style={{ overflow: 'hidden', minHeight: 0 }}>
          <div className="wf-rail" style={{ width: 60 }}>
            {AUDIT_ENTRIES.map((_, i) => (
              <span key={i} className={"wf-rail-dot " + (AUDIT_ENTRIES[i].tone || '')} style={{ top: `${(i + 0.5) * 42}px` }} />
            ))}
          </div>
          <div className="fl c" style={{ overflow: 'hidden' }}>
            {AUDIT_ENTRIES.map((e, i) => (
              <div key={i} className="r" style={{ gridTemplateColumns: '160px 110px 1fr 60px', display: 'grid', padding: '12px 14px', height: 42, borderBottom: i < AUDIT_ENTRIES.length - 1 ? '1px solid var(--wf-stroke-soft)' : 0, gap: 12, alignItems: 'center' }}>
                <div className="wf-tx-mono wf-tx" style={{ fontSize: 11, fontWeight: 600 }}>{e.k}</div>
                <div className="r g6">
                  <span className={"wf-av " + (e.a === 'system' ? 'sys' : e.a === 'user' ? '' : 'agent')} style={{ width: 14, height: 14 }} />
                  <span className="wf-tx-3" style={{ fontSize: 11 }}>{e.a}</span>
                </div>
                <div className="wf-tx-2 wf-tx-mono" style={{ fontSize: 10.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.summary}</div>
                <div className="wf-tx-4 wf-tx-mono" style={{ fontSize: 10 }}>{e.ts}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </WFShell>
  );
}

// ─── B: Pure table, expanded JSON inline ───────────────────────────────
function AuditB() {
  return (
    <WFShell active="tix" crumbs={[{ label: 'Tickets' }, { label: '#2431', active: true }]}>
      <WFNote tag="B · TABLE" title="Plain rows; one row expanded to show the full Pydantic payload">
        No rail; pure log-viewer. Faster scan for filters, but loses the visual
        progression of time.
      </WFNote>
      <TicketHeader active="audit" />
      <FilterRow />
      <div className="wf-box fl c" style={{ minHeight: 0, overflow: 'hidden' }}>
        <div className="wf-thead" style={{ gridTemplateColumns: '16px 80px 180px 100px 1fr 70px' }}>
          <div /><div>ts</div><div>kind</div><div>actor</div><div>summary</div><div>cost</div>
        </div>
        <div className="c" style={{ overflow: 'hidden' }}>
          {AUDIT_ENTRIES.slice(0, 5).map((e, i) => (
            <React.Fragment key={i}>
              <div className="wf-trow" style={{ gridTemplateColumns: '16px 80px 180px 100px 1fr 70px' }}>
                <div className="wf-tx-3" style={{ fontSize: 10 }}>{i === 0 ? '▾' : '▸'}</div>
                <div className="wf-tx-mono wf-tx-3" style={{ fontSize: 10.5 }}>{e.ts}</div>
                <div className="wf-tx-mono wf-tx" style={{ fontSize: 11, fontWeight: 600 }}>{e.k}</div>
                <div className="r g6"><span className={"wf-av " + (e.a === 'system' ? 'sys' : 'agent')} style={{ width: 12, height: 12 }} /><span className="wf-tx-3" style={{ fontSize: 10.5 }}>{e.a}</span></div>
                <div className="wf-tx-2 wf-tx-mono" style={{ fontSize: 10.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.summary}</div>
                <div className="wf-tx-3 wf-tx-mono" style={{ fontSize: 10 }}>{i === 0 ? '$0.14' : '—'}</div>
              </div>
              {i === 0 && (
                <div style={{ padding: '10px 14px 14px 38px', background: 'var(--wf-fill-2)', borderBottom: '1px solid var(--wf-stroke-soft)' }}>
                  <div className="wf-code">{`{
  "kind": "review_job.posted",
  "ts":   "2026-05-15T18:42:08.114Z",
  "actor": { "kind": "agent", "name": "style" },
  "payload": {
    "agent":          "style",
    "verdict":        "APPROVED",
    "findings_count": 2,
    "duration_ms":    184000,
    "tokens_in":      11840,
    "tokens_out":     720,
    "cost_usd":       0.14
  }
}`}</div>
                </div>
              )}
            </React.Fragment>
          ))}
        </div>
      </div>
    </WFShell>
  );
}

// ─── C: Grouped per agent / job lane ───────────────────────────────────
function AuditC() {
  const lanes = [
    { name: 'arch', tone: 'acc',  entries: ['scheduled', 'started', 'prompt_sent', 'step → invoking_agent', 'heartbeat ok'] },
    { name: 'sec',  tone: '',     entries: ['scheduled', 'queued (worker pool full)'] },
    { name: 'style',tone: 'ok',   entries: ['scheduled', 'started', 'prompt_sent', 'lessons.read', 'step → posting', 'step → posted', 'posted · APPROVED'] },
  ];
  return (
    <WFShell active="tix" crumbs={[{ label: 'Tickets' }, { label: '#2431', active: true }]}>
      <WFNote tag="C · LANES" title="One swimlane per review job, events flow left-to-right">
        Clearer per-agent narrative; harder for cross-cutting filters
        (e.g. 'show all heartbeats').
      </WFNote>
      <TicketHeader active="audit" />
      <FilterRow />
      <div className="c g12 fl" style={{ minHeight: 0, overflow: 'hidden' }}>
        {lanes.map((l) => (
          <div key={l.name} className="wf-block c g8">
            <div className="r between">
              <div className="r g8">
                <span className="wf-av agent" />
                <span style={{ fontWeight: 600 }}>{l.name}</span>
                {l.tone === 'acc' && <span className="wf-chip acc"><span className="wf-pulse" style={{ width: 6, height: 6 }} />running</span>}
                {l.tone === 'ok' && <span className="wf-chip solid-ok">APPROVED</span>}
              </div>
              <span className="wf-tx-3 wf-tx-mono" style={{ fontSize: 10 }}>{l.entries.length} entries</span>
            </div>
            <div className="r g0" style={{ position: 'relative', padding: '8px 0' }}>
              <div style={{ position: 'absolute', left: 6, right: 6, top: '50%', height: 1, background: 'var(--wf-stroke-soft)' }} />
              {l.entries.map((e, j) => (
                <div key={j} className="c g6 center" style={{ flex: 1, position: 'relative', minWidth: 0 }}>
                  <div style={{ width: 10, height: 10, borderRadius: '50%', border: '1.5px solid var(--wf-stroke)', background: 'white', zIndex: 1 }} />
                  <div className="wf-tx-mono wf-tx-2" style={{ fontSize: 9.5, textAlign: 'center', padding: '0 4px', overflow: 'hidden' }}>{e}</div>
                </div>
              ))}
            </div>
          </div>
        ))}
        <div className="fl" />
      </div>
    </WFShell>
  );
}

Object.assign(window, { AuditA, AuditB, AuditC });
