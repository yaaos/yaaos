// app/screens-ticket-detail.jsx — Ticket detail: Review + Audit log tabs

function TicketDetailHeader({ ticket, onRereview, onCancel }) {
  return (
    <div className="page-h" style={{ alignItems: 'flex-start' }}>
      <div className="c g8" style={{ minWidth: 0, maxWidth: '70%' }}>
        <div className="r g8">
          <span className="t4 mono fz11">#{ticket.number}</span>
          <span className="t4">·</span>
          <span className="t3 mono fz11">{ticket.repo}</span>
        </div>
        <h1 style={{ marginTop: 0 }}>{ticket.title}</h1>
        <div className="r g6 wrap">
          <StatusBadge status={ticket.status} />
          <KindChip kind={ticket.kind} />
          {ticket.pr.is_draft && <span className="chip" style={{ color: 'var(--text-4)' }}>draft</span>}
        </div>
        <SourceLine ticket={ticket} />
      </div>
      <div className="r g8" style={{ paddingTop: 4 }}>
        <button className="btn" onClick={onCancel}>
          <Icons.X width={13} height={13} />
          Cancel jobs
        </button>
        <button className="btn btn-primary" onClick={onRereview}>
          <Icons.Replay width={13} height={13} />
          Re-review
        </button>
      </div>
    </div>
  );
}

function TicketTabs({ active, ticketId, audit, review }) {
  return (
    <div className="tabs">
      <Link to={`/tickets/${ticketId}/review`} className={"tab " + (active === 'review' ? 'active' : '')}>
        Review
        <span className="count">{review}</span>
      </Link>
      <Link to={`/tickets/${ticketId}/audit`} className={"tab " + (active === 'audit' ? 'active' : '')}>
        Audit log
        <span className="count">{audit}</span>
      </Link>
    </div>
  );
}

// ─── REVIEW TAB ─────────────────────────────────────────────────────
function ReviewTab({ ticket, jobs, agents, lessons, expandedFinding, setExpandedFinding }) {
  const order = ['arch', 'sec', 'style'];
  return (
    <div className="c g14">
      <SummaryStrip ticket={ticket} jobs={jobs} />
      <div className="c g14">
        {order.map((agentId) => (
          <AgentCard
            key={agentId}
            ticketId={ticket.id}
            agent={agents.find((a) => a.id === agentId)}
            job={jobs[agentId]}
            lessons={lessons[ticket.repo_id] || []}
            expandedFinding={expandedFinding}
            setExpandedFinding={setExpandedFinding}
          />
        ))}
      </div>
    </div>
  );
}

function SummaryStrip({ ticket, jobs }) {
  const totalCost = ['arch','sec','style'].reduce((s, k) => s + (jobs[k]?.cost_usd || 0), 0);
  const totalTokens = ['arch','sec','style'].reduce((s, k) => s + (jobs[k]?.tokens_in || 0) + (jobs[k]?.tokens_out || 0), 0);
  const findingsCount = ['arch','sec','style'].reduce((s, k) => s + (jobs[k]?.findings?.length || 0), 0);
  const mustFix = ['arch','sec','style'].reduce((s, k) => s + (jobs[k]?.findings?.filter((f) => f.severity === 'must-fix').length || 0), 0);
  return (
    <div className="card r" style={{ overflow: 'hidden' }}>
      {[
        { l: 'Findings', v: findingsCount, sub: mustFix > 0 ? `${mustFix} must-fix` : 'no must-fixes', tone: mustFix > 0 ? 'danger' : 'soft' },
        { l: 'Total cost',   v: fmtCost(totalCost),         sub: '3 jobs' },
        { l: 'Tokens',       v: fmtTokens(totalTokens),     sub: 'in + out' },
        { l: 'Latency',      v: <LiveLatency ticket={ticket} />, sub: 'longest job' },
        { l: 'Lessons',      v: jobs.style?.lessons_applied?.length || 0, sub: `from ${ticket.repo}` },
      ].map((m, i) => (
        <div key={i} className="r g14 fl" style={{ padding: '12px 16px', borderLeft: i ? '1px solid var(--border-soft)' : 0, minWidth: 0 }}>
          <div className="c g4 fl">
            <div className="sec-h">{m.l}</div>
            <div className="r g6 baseline">
              <div className="mono fw6" style={{ fontSize: 17, color: m.tone === 'danger' ? 'var(--danger)' : 'var(--text)' }}>{m.v}</div>
              <span className="t4 fz11">{m.sub}</span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function LiveLatency({ ticket }) {
  const now = useNow(1000);
  if (!ticket.is_live) return <span className="mono">3m 02s</span>;
  // count from ticket created — to a max of 10m for the demo
  const elapsed = Math.min((now - ticket.created), 600_000);
  const s = Math.floor(elapsed / 1000);
  return <span className="mono" style={{ color: 'var(--accent)' }}>{Math.floor(s / 60)}m {String(s % 60).padStart(2, '0')}s</span>;
}

function AgentCard({ ticketId, agent, job, lessons, expandedFinding, setExpandedFinding }) {
  if (!job) return null;
  const isRunning = job.status === 'running';
  const isQueued  = job.status === 'queued';
  const isPosted  = job.status === 'posted';
  const isSkipped = job.status === 'skipped';

  return (
    <div className="card" style={{ position: 'relative', borderColor: isRunning ? 'var(--accent-border)' : 'var(--border-soft)', boxShadow: isRunning ? 'var(--shadow-glow)' : 'none', transition: 'border-color 0.3s, box-shadow 0.3s' }}>
      <div className="card-h" style={{ borderBottom: '1px solid var(--border-soft)' }}>
        <div className="av agent" style={{ width: 22, height: 22, borderRadius: 5, fontSize: 11 }}>{agent.name.slice(0, 1)}</div>
        <div className="c" style={{ minWidth: 0 }}>
          <div className="r g8">
            <div className="fw6" style={{ fontSize: 13.5 }}>{agent.name}</div>
            <span className="chip mono" style={{ fontSize: 9.5, padding: '0 5px' }}>agent · {agent.id}</span>
          </div>
          <div className="t3 fz11 mono">{job.prompt_hash || '—'} · model claude-sonnet-4-5 · {job.lessons_applied?.length || 0} lessons applied</div>
        </div>
        <div className="fl" />
        <div className="r g8">
          {isRunning && <span className="badge badge-accent"><span className="pulse-dot" style={{ width: 6, height: 6 }} />Running</span>}
          {isQueued  && <span className="badge badge-soft"><span className="dot" style={{ background: 'var(--text-3)' }} />Queued</span>}
          {isPosted  && <VerdictBadge verdict={job.verdict} />}
          {isSkipped && <span className="badge badge-soft" style={{ color: 'var(--text-4)' }}><span className="dot" />Skipped</span>}
          <button className="btn btn-sm" title="View prompt">View prompt</button>
        </div>
      </div>

      {isRunning && (
        <div className="card-b" style={{ paddingBottom: 12, borderBottom: '1px solid var(--border-soft)' }}>
          <RunningSection job={job} />
        </div>
      )}
      {isQueued && (
        <div className="card-b" style={{ borderBottom: '1px solid var(--border-soft)' }}>
          <div className="r g10">
            <div className="spinner" style={{ borderTopColor: 'var(--text-3)' }} />
            <div className="t2 fz12">{job.step_label}</div>
            <div className="fl" />
            <div className="t4 fz11 mono">Worker pool · 2 of 4 busy</div>
          </div>
        </div>
      )}

      {isPosted && (
        <div className="card-b" style={{ paddingTop: 10, paddingBottom: job.findings.length ? 10 : 14, borderBottom: job.findings.length ? '1px solid var(--border-soft)' : 0 }}>
          <PostedMeta job={job} />
        </div>
      )}

      {(isPosted || isRunning) && job.findings && job.findings.length > 0 && (
        <div className="c" style={{ padding: '4px 8px 8px' }}>
          {job.findings.map((f) => (
            <FindingRow
              key={f.id}
              finding={f}
              agent={agent}
              lesson={lessons.find((l) => l.id === f.applied_lesson)}
              expanded={expandedFinding === f.id}
              onToggle={() => setExpandedFinding(expandedFinding === f.id ? null : f.id)}
            />
          ))}
        </div>
      )}

      {isPosted && job.findings.length === 0 && (
        <div className="card-b" style={{ paddingTop: 14 }}>
          <div className="r g8 t3 fz12">
            <Icons.CheckCircle width={14} height={14} style={{ color: 'var(--success)' }} />
            <span>No findings — clean from {agent.name.toLowerCase()}'s perspective.</span>
          </div>
        </div>
      )}
    </div>
  );
}

function RunningSection({ job }) {
  const now = useNow(500);
  // Inflate the displayed progress slightly over time so it feels alive
  const wobble = 0.04 * Math.sin(now / 700);
  const progress = Math.min(0.96, (job.progress || 0.62) + wobble);
  return (
    <div className="c g8">
      <div className="r g10">
        <span className="pulse-dot" />
        <div className="fw6 fz13">{job.step_label}</div>
        <div className="fl" />
        <span className="t3 mono fz11">heartbeat {job.heartbeat_age_s}s ago · ok</span>
      </div>
      <div className="bar indeterminate"><span className="bar-fill" style={{ width: `${progress * 100}%` }} /></div>
      <div className="r g16 wrap t3 fz11 mono">
        <span><Icons.Clock width={11} height={11} style={{ verticalAlign: -2, marginRight: 3, opacity: 0.6 }} />elapsed <LiveElapsed since={job.started} /></span>
        <span><Icons.Token width={11} height={11} style={{ verticalAlign: -2, marginRight: 3, opacity: 0.6 }} />tokens <LiveCounter base={job.tokens_in} rate={6} /> in · <LiveCounter base={job.tokens_out} rate={0.6} /> out</span>
        <span><Icons.Coin width={11} height={11} style={{ verticalAlign: -2, marginRight: 3, opacity: 0.6 }} />cost <LiveCost base={job.cost_usd} /></span>
      </div>
    </div>
  );
}

function LiveElapsed({ since }) {
  const now = useNow(1000);
  if (!since) return <span>—</span>;
  const s = Math.floor((now - since) / 1000);
  return <span>{Math.floor(s / 60)}m {String(s % 60).padStart(2, '0')}s</span>;
}
function LiveCounter({ base, rate = 1 }) {
  const now = useNow(700);
  const extra = Math.floor((now % 60_000) / 700) * rate * 4;
  return <span>{fmtTokens(Math.floor((base || 0) + extra))}</span>;
}
function LiveCost({ base }) {
  const now = useNow(1500);
  const extra = ((now % 60_000) / 60_000) * 0.04;
  return <span>{fmtCost((base || 0) + extra)}</span>;
}

function PostedMeta({ job }) {
  return (
    <div className="r g16 wrap t3 fz11 mono">
      <span className="r g6"><Icons.Clock width={11} height={11} style={{ opacity: 0.6 }} />posted <RelText ts={job.posted} /></span>
      <span className="r g6"><Icons.Bolt width={11} height={11} style={{ opacity: 0.6 }} />{durationMs(job.duration_s * 1000)}</span>
      <span className="r g6"><Icons.Token width={11} height={11} style={{ opacity: 0.6 }} />{fmtTokens(job.tokens_in)} in · {fmtTokens(job.tokens_out)} out</span>
      <span className="r g6"><Icons.Coin width={11} height={11} style={{ opacity: 0.6 }} />{fmtCost(job.cost_usd)}</span>
      <span className="fl" />
      <span className="r g4">
        <a className="r g4" style={{ borderBottom: '1px dashed var(--text-4)', color: 'var(--text-2)' }} href="#">view on GitHub <Icons.External width={10} height={10} /></a>
      </span>
    </div>
  );
}
function RelText({ ts }) {
  const now = useNow(15000);
  return <span>{relTime(ts, now)}</span>;
}

// ─── FINDING ROW ───────────────────────────────────────────────────
function FindingRow({ finding, agent, lesson, expanded, onToggle }) {
  return (
    <div
      className="c g8"
      style={{
        padding: expanded ? 12 : '10px 12px',
        borderRadius: 8,
        margin: '2px 0',
        background: expanded ? 'var(--surface-2)' : 'transparent',
        border: '1px solid ' + (expanded ? 'var(--border)' : 'transparent'),
        transition: 'background 0.12s, border-color 0.12s, padding 0.12s',
      }}
    >
      <div className="r g10 start" onClick={onToggle} style={{ cursor: 'default' }}>
        <SevDot sev={finding.severity} />
        <div className="c g4 fl" style={{ minWidth: 0 }}>
          <div className="r g8 baseline">
            <span className="fw6 fz13 t1 ellip" style={{ minWidth: 0 }}>{finding.title}</span>
            <span className={"chip " + (finding.severity === 'must-fix' ? 'mono' : 'mono')} style={{
              fontSize: 9.5,
              padding: '0 6px',
              color:
                finding.severity === 'must-fix' ? 'var(--danger)' :
                finding.severity === 'nit'      ? 'var(--warning)' :
                finding.severity === 'suggestion' ? 'var(--info)' : 'var(--text-3)',
              background:
                finding.severity === 'must-fix' ? 'var(--danger-bg)' :
                finding.severity === 'nit'      ? 'var(--warning-bg)' :
                finding.severity === 'suggestion' ? 'var(--info-bg)' : 'var(--surface-2)',
              borderColor: 'transparent',
            }}>{finding.severity}</span>
          </div>
          <div className="t3 fz11 mono ellip">{finding.file}:{finding.line}</div>
          {!expanded && <div className="t2 fz12 ellip">{finding.body}</div>}
        </div>
        <Icons.ChevronDown
          width={14} height={14}
          style={{
            color: 'var(--text-3)',
            transform: expanded ? 'rotate(180deg)' : 'rotate(0deg)',
            transition: 'transform 0.18s',
            flex: 'none',
            marginTop: 4,
          }}
        />
      </div>
      {expanded && (
        <div className="c g10" style={{ marginLeft: 18 }}>
          <div className="t2 fz12" style={{ lineHeight: 1.6 }}>{finding.body}</div>
          {finding.snippet && (
            <div className="c g4">
              <div className="sec-h">Suggested change</div>
              <CodeSnippet snippet={finding.snippet} />
            </div>
          )}
          {finding.rationale && (
            <div className="c g4">
              <div className="sec-h">Agent rationale</div>
              <div className="t2 fz12" style={{ fontStyle: 'italic', lineHeight: 1.55 }}>"{finding.rationale}"</div>
            </div>
          )}
          {lesson && (
            <div className="r g8 baseline" style={{ padding: '6px 10px', background: 'var(--accent-bg-2)', border: '1px solid var(--accent-border)', borderRadius: 6 }}>
              <Icons.Sparkle width={12} height={12} style={{ color: 'var(--accent)' }} />
              <span className="t3 fz11" style={{ textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600 }}>Applied lesson</span>
              <Link to="/memory" className="t1 fw5 fz12">{lesson.title}</Link>
              <span className="t4 mono fz11">· {agent.name.toLowerCase()} read it from acme/web</span>
            </div>
          )}
          <div className="r g6">
            <button className="btn btn-sm btn-primary">
              <Icons.Check width={12} height={12} />
              Resolve
            </button>
            <button className="btn btn-sm">
              <Icons.Reply width={12} height={12} />
              Reply on GitHub
            </button>
            <button className="btn btn-sm btn-ghost">
              <Icons.X width={12} height={12} />
              Dismiss
            </button>
            <button className="btn btn-sm btn-ghost">
              <Icons.Wand width={12} height={12} />
              Teach yaaof…
            </button>
            <div className="fl" />
            <a href="#" className="t3 fz11 r g4">view on GitHub <Icons.External width={10} height={10} /></a>
          </div>
        </div>
      )}
    </div>
  );
}

function CodeSnippet({ snippet }) {
  return (
    <div className="code">
      {snippet.map((line, i) => {
        const text = line.text;
        const cls = line.type === 'add' ? 'add' : line.type === 'del' ? 'del' : '';
        const prefix = line.type === 'add' ? '+' : line.type === 'del' ? '−' : ' ';
        return (
          <div key={i} className={cls} style={{ padding: '0 6px', margin: '0 -12px', display: 'flex' }}>
            <span className="ln">{line.ln}</span>
            <span style={{ width: 14, color: line.type === 'add' ? 'var(--success)' : line.type === 'del' ? 'var(--danger)' : 'var(--text-4)' }}>{prefix}</span>
            <span>{text}</span>
          </div>
        );
      })}
    </div>
  );
}

// ─── AUDIT TAB ─────────────────────────────────────────────────────
function AuditTab({ entries, ticket }) {
  const [filter, setFilter] = useState('all');
  const [openId, setOpenId] = useState(entries[0]?.id || null);

  const kinds = useMemo(() => {
    const m = new Map();
    entries.forEach((e) => {
      const k = e.kind.split('.')[0];
      m.set(k, (m.get(k) || 0) + 1);
    });
    return Array.from(m.entries());
  }, [entries]);

  const filtered = filter === 'all'
    ? entries
    : entries.filter((e) => e.kind.startsWith(filter + '.'));

  return (
    <div className="c g14">
      <div className="r g6 wrap">
        <button
          className={"badge " + (filter === 'all' ? 'badge-accent' : 'badge-soft')}
          style={{ cursor: 'default', height: 22 }}
          onClick={() => setFilter('all')}
        >
          All <span className="mono" style={{ marginLeft: 4, opacity: 0.7 }}>{entries.length}</span>
        </button>
        {kinds.map(([k, n]) => (
          <button
            key={k}
            className={"badge " + (filter === k ? 'badge-accent' : 'badge-soft')}
            style={{ cursor: 'default', height: 22 }}
            onClick={() => setFilter(k)}
          >
            {k} <span className="mono" style={{ marginLeft: 4, opacity: 0.7 }}>{n}</span>
          </button>
        ))}
        <div className="fl" />
        <span className="t4 fz11 mono">{filtered.length} entries · ticket {ticket.id}</span>
      </div>
      <div className="card" style={{ overflow: 'hidden' }}>
        <div className="thead" style={{ gridTemplateColumns: '14px 60px 220px 1fr 100px 80px' }}>
          <div />
          <div>When</div>
          <div>Kind</div>
          <div>Summary</div>
          <div>Actor</div>
          <div>Cost</div>
        </div>
        <div>
          {filtered.map((e, i) => (
            <AuditRow
              key={e.id}
              entry={e}
              expanded={openId === e.id}
              onToggle={() => setOpenId(openId === e.id ? null : e.id)}
              flash={i === 0}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function AuditRow({ entry, expanded, onToggle, flash }) {
  const tone =
    entry.kind === 'review_job.posted' ? (entry.payload?.verdict === 'CHANGES_REQUESTED' ? 'danger' : 'success') :
    entry.kind === 'review_job.prompt_sent' ? 'accent' :
    entry.kind === 'review_job.step_changed' ? 'accent' :
    entry.kind === 'ticket.created' ? 'accent' :
    'soft';
  const dotColor =
    tone === 'success' ? 'var(--success)' :
    tone === 'danger'  ? 'var(--danger)' :
    tone === 'accent'  ? 'var(--accent)' : 'var(--text-3)';

  return (
    <>
      <div
        className={"trow " + (flash ? 'flash-new' : '')}
        style={{ gridTemplateColumns: '14px 60px 220px 1fr 100px 80px', cursor: 'default' }}
        onClick={onToggle}
      >
        <span style={{ width: 8, height: 8, borderRadius: 2, background: dotColor, alignSelf: 'center' }} />
        <span className="t4 mono fz11"><RelText ts={entry.ts} /></span>
        <span className="t1 mono fz12 fw5 ellip">{entry.kind}</span>
        <span className="t2 fz12 mono ellip">{summarize(entry)}</span>
        <span className="r g6">
          {entry.actor.kind === 'system' && <><Avatar kind="system" size={14} /><span className="t3 fz11">system</span></>}
          {entry.actor.kind === 'agent' && <><Avatar kind="agent" name={entry.actor.name} size={14} /><span className="t3 fz11">{entry.actor.name}</span></>}
          {entry.actor.kind === 'github_user' && <><Avatar name={entry.actor.login} size={14} /><span className="t3 fz11 ellip">{entry.actor.login}</span></>}
        </span>
        <span className="r g4 fz11 mono t3">
          {entry.payload?.cost_usd != null ? fmtCost(entry.payload.cost_usd) : '—'}
          <Icons.ChevronDown
            width={11} height={11}
            style={{ color: 'var(--text-4)', transform: expanded ? 'rotate(180deg)' : 'rotate(0)', transition: 'transform 0.18s', marginLeft: 'auto' }}
          />
        </span>
      </div>
      {expanded && (
        <div style={{ padding: '8px 16px 14px 38px', background: 'var(--bg-2)', borderBottom: '1px solid var(--border-soft)' }}>
          <div className="code" style={{ background: 'var(--surface-2)' }}>{prettyJson(entry)}</div>
        </div>
      )}
    </>
  );
}

function summarize(entry) {
  const p = entry.payload || {};
  switch (entry.kind) {
    case 'review_job.posted':
      return `${p.agent}: ${p.verdict} · ${p.findings_count} findings · ${durationMs(p.duration_ms)}${p.must_fix ? ` · ${p.must_fix} must-fix` : ''}`;
    case 'review_job.step_changed':
      return `${p.agent}: ${p.from} → ${p.to}${p.heartbeat_age_s != null ? ` · heartbeat ${p.heartbeat_age_s}s` : ''}`;
    case 'review_job.heartbeat':
      return `${p.agent} heartbeat ${p.heartbeat_age_s}s · ${p.ok ? 'ok' : 'stale'}`;
    case 'review_job.prompt_sent':
      return `${p.agent}: prompt ${p.prompt_hash} · ${fmtTokens(p.tokens_in)} tokens · ${p.lessons_count} lessons applied`;
    case 'review_job.started':
      return `${p.agent}: worker ${p.worker_id} · queue wait ${(p.queue_wait_ms / 1000).toFixed(2)}s`;
    case 'review_job.scheduled':
      return `${(p.agents || []).join(', ')} scheduled · reason ${p.reason}`;
    case 'lessons.read':
      return `${p.repo}: ${p.lessons_count} lessons read`;
    case 'ticket.created':
      return `Ticket created from ${p.source} ${p.pr_number ? '#' + p.pr_number : ''} on ${p.repo}`;
    default: return JSON.stringify(p);
  }
}

function prettyJson(entry) {
  const obj = {
    kind: entry.kind,
    ts: new Date(entry.ts).toISOString(),
    actor: entry.actor,
    payload: entry.payload,
  };
  return JSON.stringify(obj, null, 2);
}

// ─── Screen ─────────────────────────────────────────────────────────
function ScreenTicket({ route }) {
  const data = window.YAAOF_DATA;
  const ticket = data.tickets.find((t) => t.id === route.id) || data.tickets[0];
  const jobs = data.reviewJobs[ticket.id] || data.reviewJobs.t1;
  const audit = data.audit[ticket.id] || [];
  const tab = route.tab || 'review';
  const [expandedFinding, setExpandedFinding] = useState('f2'); // default the second style finding open

  // crumbs
  useEffect(() => {
    window.__ticketTitle = ticket.title;
  }, [ticket]);

  return (
    <div className="page" style={{ maxWidth: 1320 }}>
      <TicketDetailHeader ticket={ticket} onRereview={() => alert('Re-review queued (mock)')} onCancel={() => alert('Cancel jobs (mock)')} />
      <TicketTabs active={tab} ticketId={ticket.id} audit={audit.length} review={3} />
      {tab === 'review' ? (
        <ReviewTab
          ticket={ticket}
          jobs={jobs}
          agents={data.agents}
          lessons={data.lessons}
          expandedFinding={expandedFinding}
          setExpandedFinding={setExpandedFinding}
        />
      ) : (
        <AuditTab entries={audit} ticket={ticket} />
      )}
    </div>
  );
}

Object.assign(window, { ScreenTicket });
