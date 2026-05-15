// wires-detail-agents.jsx — ticket detail: Agents tab variants

function TicketHeader({ active = 'review' }) {
  return (
    <>
      <div className="wf-page-h">
        <div className="c g8">
          <div className="r g8">
            <span className="wf-tx-mono wf-tx-3" style={{ fontSize: 11 }}>#2431</span>
            <span className="wf-tx-mono wf-tx-3" style={{ fontSize: 11 }}>·</span>
            <span className="wf-tx-mono wf-tx-3" style={{ fontSize: 11 }}>acme/web</span>
          </div>
          <h1 className="wf-h1">Add real-time SSE handling to ticket detail</h1>
          <div className="r g6">
            <StatusChip status="review" />
            <KindChip kind="new feature" />
          </div>
          <SourceLine
            source="github_pr"
            refId="#2431"
            repo="acme/web"
            actor="rachel-cohen"
            verb="opened"
            age="4m ago"
            meta="feat/ticket-sse → main · +247 −38 in 11 files"
          />
        </div>
        <div className="r g8 start" style={{ paddingTop: 4 }}>
          <button className="wf-btn">Cancel jobs</button>
          <button className="wf-btn wf-btn-primary">Re-review</button>
        </div>
      </div>
      <div className="wf-tabs">
        <div className={"wf-tab" + (active === 'review' ? ' act' : '')}>Review <span className="ct">3</span></div>
        <div className={"wf-tab" + (active === 'audit' ? ' act' : '')}>Audit log <span className="ct">10</span></div>
      </div>
    </>
  );
}

// ─── A: Three columns, equal cards ─────────────────────────────────────
function AgentsA() {
  const agents = [
    { name: 'Architecture', key: 'arch', state: 'running',  verdict: null, step: 'invoking_agent', progress: 0.62, tokens: '14.8k in · 1.2k out', cost: '$0.18', latency: 't+90s', findings: 0 },
    { name: 'Security',     key: 'sec',  state: 'queued',   verdict: null, step: 'queued — waiting for worker slot', progress: 0, tokens: '— · —', cost: '$0.00', latency: '—', findings: 0 },
    { name: 'Style',        key: 'style',state: 'posted',   verdict: 'APPROVED', step: 'posted 35s ago', progress: 1, tokens: '11.8k in · 720 out', cost: '$0.14', latency: '184s', findings: 2 },
  ];
  return (
    <WFShell active="tix" crumbs={[{ label: 'Tickets' }, { label: '#2431', active: true }]}>
      <WFNote tag="A · 3 COLUMNS" title="One card per agent, equal-width row">
        Quick parallel scan of all three agents. Findings are summarized; full
        finding details live in an expandable section or popover.
      </WFNote>
      <TicketHeader active="review" />
      <div className="r g12 fl start" style={{ minHeight: 0 }}>
        {agents.map((a) => (
          <div key={a.key} className={"wf-agent fl " + a.state}>
            <div className="r between">
              <div className="r g8">
                <span className="wf-av agent" />
                <div className="c">
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{a.name}</div>
                  <div className="wf-tx-mono wf-tx-3" style={{ fontSize: 10 }}>review-agent · {a.key}</div>
                </div>
              </div>
              {a.verdict ? <VerdictChip verdict={a.verdict} />
                : a.state === 'running' ? <span className="wf-chip acc"><span className="wf-pulse" style={{ width: 6, height: 6 }} />running</span>
                : <span className="wf-chip"><span className="dot" />queued</span>}
            </div>
            <div className="c g6">
              <div className="wf-tx-2" style={{ fontSize: 11 }}>{a.step}</div>
              <div className={"wf-progress " + (a.state === 'running' ? 'indet' : '')}>
                <div style={{ width: `${a.progress * 100}%` }} />
              </div>
            </div>
            <div className="r between wf-tx-mono wf-tx-3" style={{ fontSize: 10 }}>
              <span>{a.latency}</span>
              <span>{a.tokens}</span>
              <span>{a.cost}</span>
            </div>
            <div className="c g6" style={{ borderTop: '1px solid var(--wf-stroke-soft)', paddingTop: 8 }}>
              <div className="r between">
                <div className="wf-sec-h">Findings</div>
                <span className="wf-tx-3" style={{ fontSize: 10 }}>{a.findings}</span>
              </div>
              {a.findings === 0 ? (
                <div className="wf-tx-4" style={{ fontSize: 11 }}>{a.state === 'running' ? 'pending…' : a.state === 'queued' ? '—' : 'none'}</div>
              ) : (
                <div className="c g6">
                  <div className="r g6 start">
                    <span className="wf-sev nit" style={{ marginTop: 4 }} />
                    <div className="c g4 fl">
                      <div className="wf-tx-mono wf-tx-3" style={{ fontSize: 10 }}>src/routes/ticket.tsx:42</div>
                      <div className="wf-tx" style={{ fontSize: 11.5 }}>Prefer named export for route component</div>
                    </div>
                  </div>
                  <div className="r g6 start">
                    <span className="wf-sev info" style={{ marginTop: 4 }} />
                    <div className="c g4 fl">
                      <div className="wf-tx-mono wf-tx-3" style={{ fontSize: 10 }}>src/lib/sse.ts:88</div>
                      <div className="wf-tx" style={{ fontSize: 11.5 }}>Reconnect backoff could reference RECONNECT_BASE_MS</div>
                    </div>
                  </div>
                </div>
              )}
            </div>
            <div className="r g6">
              <button className="wf-btn-ghost wf-btn" style={{ fontSize: 10 }}>View prompt</button>
              <button className="wf-btn-ghost wf-btn" style={{ fontSize: 10 }}>Open comment ↗</button>
            </div>
          </div>
        ))}
      </div>
    </WFShell>
  );
}

// ─── B: Vertical stack, findings inline ────────────────────────────────
function AgentsB() {
  const agents = [
    { name: 'Style',        verdict: 'APPROVED', state: 'posted',  meta: 'posted 35s ago · 184s · $0.14',
      findings: [
        { f: 'src/routes/ticket.tsx', l: 42, sev: 'nit', t: 'Prefer named export for route component', b: 'Other route components in src/routes/ use named exports. Match the local convention.' },
        {
          f: 'src/lib/sse.ts', l: 88, sev: 'info',
          t: 'Reconnect backoff could reference the constant',
          b: 'You hardcode 2000. The same value lives in constants.ts as RECONNECT_BASE_MS.',
          expanded: true,
          snippet: `82  const stream = new EventSource(url);
83  stream.onerror = () => {
84    if (retries > MAX_RETRIES) return abort();
85    retries++;
86 -  setTimeout(connect, 2000);
86 +  setTimeout(connect, RECONNECT_BASE_MS);
87  };`,
          rationale: "Reusing the constant means the reconnect base will track future tuning in constants.ts. Self-documenting; one less magic number in the diff.",
          lesson_ref: 'l2 · Use @/lib/queries for server state',
        },
      ] },
    { name: 'Architecture', verdict: null, state: 'running', meta: 'running · t+90s · invoking claude-code · $0.18 so far' },
    { name: 'Security',     verdict: null, state: 'queued',  meta: 'queued · worker pool: 2 / 4 busy' },
  ];
  return (
    <WFShell active="tix" crumbs={[{ label: 'Tickets' }, { label: '#2431', active: true }]}>
      <WFNote tag="B · VERTICAL" title="One agent per row, findings inline · click to expand">
        Findings show file:line + title + body collapsed; click any finding to
        expand → code snippet with the suggested edit, agent's rationale, lesson
        reference (if any), and row-level actions (resolve · dismiss · reply).
        See the 2nd finding for the expanded state.
      </WFNote>
      <TicketHeader active="review" />
      <div className="c g12 fl" style={{ minHeight: 0, overflow: 'hidden' }}>
        {agents.map((a, i) => (
          <div key={i} className={"wf-agent " + a.state}>
            <div className="r between">
              <div className="r g10">
                <span className="wf-av agent" />
                <div className="c">
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{a.name}</div>
                  <div className="wf-tx-3" style={{ fontSize: 11 }}>{a.meta}</div>
                </div>
              </div>
              <div className="r g8">
                {a.verdict ? <VerdictChip verdict={a.verdict} />
                  : a.state === 'running' ? <span className="wf-chip acc"><span className="wf-pulse" style={{ width: 6, height: 6 }} />running</span>
                  : <span className="wf-chip"><span className="dot" />queued</span>}
                <button className="wf-btn-ghost wf-btn" style={{ fontSize: 10 }}>View prompt</button>
              </div>
            </div>
            {a.state === 'running' && <div className="wf-progress indet"><div /></div>}
            {a.findings && (
              <div className="c g6" style={{ borderTop: '1px solid var(--wf-stroke-soft)', paddingTop: 8 }}>
                {a.findings.map((f, j) => (
                  <div key={j} className="r g10 start" style={{ padding: '8px 10px', background: f.expanded ? 'white' : 'var(--wf-fill-2)', border: f.expanded ? '1px solid var(--wf-stroke)' : '1px solid transparent', borderRadius: 4 }}>
                    <span className={"wf-sev " + (f.sev === 'must-fix' ? 'must' : f.sev === 'nit' ? 'nit' : f.sev === 'suggestion' ? 'sug' : 'info')} style={{ marginTop: 4 }} />
                    <div className="c g6 fl">
                      <div className="r g8 between">
                        <div className="r g8">
                          <span className="wf-tx-3" style={{ fontSize: 10, fontFamily: 'JetBrains Mono, monospace' }}>{f.expanded ? '▾' : '▸'}</span>
                          <span className="wf-tx" style={{ fontWeight: 600, fontSize: 12 }}>{f.t}</span>
                          <span className="wf-chip" style={{ fontSize: 9.5 }}>{f.sev}</span>
                        </div>
                        <div className="wf-tx-mono wf-tx-3" style={{ fontSize: 10.5 }}>{f.f}:{f.l}</div>
                      </div>
                      <div className="wf-tx-2" style={{ fontSize: 11 }}>{f.b}</div>
                      {f.expanded && (
                        <div className="c g6" style={{ marginTop: 4 }}>
                          <div className="wf-sec-h">Suggested change</div>
                          <div className="wf-code">{f.snippet}</div>
                          {f.rationale && (
                            <>
                              <div className="wf-sec-h">Agent rationale</div>
                              <div className="wf-tx-2" style={{ fontSize: 11, fontStyle: 'italic' }}>"{f.rationale}"</div>
                            </>
                          )}
                          {f.lesson_ref && (
                            <div className="r g6 wf-tx-3" style={{ fontSize: 10.5 }}>
                              <span style={{ fontSize: 9.5, textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600 }}>Applied lesson</span>
                              <span className="wf-chip" style={{ fontSize: 10 }}><span className="dot" />{f.lesson_ref}</span>
                            </div>
                          )}
                          <div className="r g6" style={{ marginTop: 2 }}>
                            <button className="wf-btn wf-btn-primary" style={{ fontSize: 10.5, height: 22 }}>Resolve</button>
                            <button className="wf-btn" style={{ fontSize: 10.5, height: 22 }}>Reply ↗</button>
                            <button className="wf-btn-ghost wf-btn" style={{ fontSize: 10.5, height: 22 }}>Dismiss</button>
                            <button className="wf-btn-ghost wf-btn" style={{ fontSize: 10.5, height: 22 }}>Teach yaaof…</button>
                            <span className="fl" />
                            <span className="wf-link wf-tx-3" style={{ fontSize: 10.5 }}>view on github ↗</span>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </WFShell>
  );
}

// ─── C: Master-detail split ────────────────────────────────────────────
function AgentsC() {
  const agents = [
    { name: 'Architecture', state: 'running',  verdict: null,         meta: 't+90s · running', sel: true },
    { name: 'Security',     state: 'queued',   verdict: null,         meta: 'queued' },
    { name: 'Style',        state: 'posted',   verdict: 'APPROVED',   meta: '35s · 2 findings' },
  ];
  return (
    <WFShell active="tix" crumbs={[{ label: 'Tickets' }, { label: '#2431', active: true }]}>
      <WFNote tag="C · MASTER-DETAIL" title="Agent list on left, expanded detail on right">
        Closer to a mail-app layout. Detail panel has more breathing room for long
        findings + prompt; agent list stays compact.
      </WFNote>
      <TicketHeader active="review" />
      <div className="r g0 fl wf-box" style={{ minHeight: 0, overflow: 'hidden' }}>
        {/* List */}
        <div className="c" style={{ width: 280, borderRight: '1px solid var(--wf-stroke)', flex: 'none' }}>
          {agents.map((a, i) => (
            <div key={i} className="c g6" style={{ padding: '12px 14px', borderBottom: i < agents.length - 1 ? '1px solid var(--wf-stroke-soft)' : 0, background: a.sel ? 'var(--wf-fill-2)' : 'white', borderLeft: a.sel ? '3px solid var(--wf-accent)' : '3px solid transparent', paddingLeft: a.sel ? 11 : 14 }}>
              <div className="r between">
                <div className="r g8">
                  <span className="wf-av agent" />
                  <span style={{ fontWeight: 600, fontSize: 12 }}>{a.name}</span>
                </div>
                {a.verdict ? <VerdictChip verdict={a.verdict} />
                  : a.state === 'running' ? <span className="wf-pulse" />
                  : <span className="wf-sev" style={{ background: 'var(--wf-fill-4)', border: '1px solid var(--wf-stroke-2)' }} />}
              </div>
              <div className="wf-tx-mono wf-tx-3" style={{ fontSize: 10 }}>{a.meta}</div>
              {a.state === 'running' && <div className="wf-progress indet"><div /></div>}
            </div>
          ))}
          <div style={{ padding: '10px 14px', borderTop: '1px solid var(--wf-stroke-soft)' }}>
            <div className="wf-sec-h">Total · this ticket</div>
            <div className="r between wf-tx-mono wf-tx-3" style={{ fontSize: 10.5, marginTop: 4 }}>
              <span>26.6k tokens</span>
              <span>$0.32</span>
            </div>
          </div>
        </div>
        {/* Detail */}
        <div className="fl c" style={{ minWidth: 0, overflow: 'hidden' }}>
          <div className="r between" style={{ padding: '14px 16px', borderBottom: '1px solid var(--wf-stroke-soft)' }}>
            <div className="r g10">
              <span className="wf-av agent" />
              <div className="c">
                <div style={{ fontWeight: 600 }}>Architecture</div>
                <div className="wf-tx-3 wf-tx-mono" style={{ fontSize: 10.5 }}>arch · prompt p_arch_8c1a · 4 lessons applied</div>
              </div>
            </div>
            <div className="r g8">
              <span className="wf-chip acc"><span className="wf-pulse" style={{ width: 6, height: 6 }} />running</span>
              <button className="wf-btn">View prompt</button>
              <button className="wf-btn">Cancel</button>
            </div>
          </div>
          <div className="c g12 fl" style={{ padding: 16, overflow: 'hidden' }}>
            <div className="wf-box-soft c g8" style={{ padding: 12 }}>
              <div className="wf-sec-h">Current step</div>
              <div className="r g8">
                <span className="wf-pulse" />
                <span className="wf-tx" style={{ fontWeight: 600 }}>Invoking coding agent</span>
                <span className="wf-tx-3 wf-tx-mono" style={{ fontSize: 10 }}>claude-code · heartbeat 4s ago</span>
              </div>
              <div className="wf-progress indet"><div /></div>
              <div className="r g16 wf-tx-mono wf-tx-3" style={{ fontSize: 10.5 }}>
                <span>started t+0s</span><span>elapsed 1m 30s</span><span>tokens 14.8k in / 1.2k out</span><span>cost $0.18</span>
              </div>
            </div>
            <div className="wf-sec-h">Findings (pending — agent is still running)</div>
            <div className="c g8 fl" style={{ minHeight: 0 }}>
              {[1,2,3].map((i) => (
                <div key={i} className="wf-block r g10 start" style={{ padding: 10, opacity: 0.55 }}>
                  <span className="wf-sev" style={{ background: 'var(--wf-fill-4)', border: '1px solid var(--wf-stroke-2)', marginTop: 4 }} />
                  <div className="c g4 fl">
                    <div className="wf-bar" style={{ width: '50%' }} />
                    <div className="wf-bar-thin" style={{ width: '70%' }} />
                    <div className="wf-bar-thin" style={{ width: '90%' }} />
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </WFShell>
  );
}

Object.assign(window, { AgentsA, AgentsB, AgentsC });
