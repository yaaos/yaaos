// app/screens-dashboard.jsx — Dashboard (populated + onboarding)

function ScreenDashboard({ onboarding, onJumpToSetup }) {
  // onboarding object: { github_app, api_key, repos } booleans
  const allDone = onboarding.github_app && onboarding.api_key && onboarding.repos;
  if (!allDone) return <DashOnboarding onboarding={onboarding} onJump={onJumpToSetup} />;
  return <DashPopulated />;
}

// ─── Onboarding (stepper + preview) ────────────────────────────────
function DashOnboarding({ onboarding, onJump }) {
  const steps = [
    { id: 'github_app', n: 1, h: 'Install the GitHub App', sub: 'Grant yaaof access to the repos you want reviewed.', cta: 'Install', done: onboarding.github_app, jumpTo: 'settings' },
    { id: 'api_key',    n: 2, h: 'Add your model API key',  sub: 'Anthropic, OpenAI, or compatible.', cta: 'Add key', done: onboarding.api_key, jumpTo: 'settings' },
    { id: 'repos',      n: 3, h: 'Add a repo to the allowlist', sub: 'yaaof opens a ticket each time a PR lands on an allowlisted repo.', cta: 'Add repo', done: onboarding.repos, jumpTo: 'repos' },
  ];
  const completed = steps.filter((s) => s.done).length;
  return (
    <div className="page" style={{ maxWidth: 980 }}>
      <div className="page-h">
        <div>
          <h1>Welcome to yaaof</h1>
          <div className="sub">Three steps to your first review.</div>
        </div>
        <span className="badge badge-soft tnum">{completed} of {steps.length} complete</span>
      </div>

      <div className="card" style={{ overflow: 'hidden' }}>
        {steps.map((s, i) => (
          <div
            key={s.id}
            className="r g14"
            style={{
              padding: '16px 20px',
              borderBottom: i < steps.length - 1 ? '1px solid var(--border-soft)' : 0,
              background: s.done ? 'var(--success-bg)' : 'transparent',
              transition: 'background 0.2s',
            }}
          >
            <div style={{
              width: 28, height: 28, borderRadius: '50%',
              border: '1.5px solid ' + (s.done ? 'var(--success)' : 'var(--border-hard)'),
              background: s.done ? 'var(--success)' : 'transparent',
              color: s.done ? 'white' : 'var(--text-2)',
              display: 'grid', placeItems: 'center',
              fontWeight: 600, fontSize: 12, flex: 'none',
            }}>
              {s.done ? <Icons.Check width={14} height={14} /> : s.n}
            </div>
            <div className="c g4 fl">
              <div className="fw6 fz13" style={{ textDecoration: s.done ? 'line-through' : 'none', color: s.done ? 'var(--text-3)' : 'var(--text)' }}>
                {s.h}
              </div>
              <div className="t3 fz12">{s.sub}</div>
            </div>
            {s.done ? (
              <span className="badge badge-success"><Icons.Check width={11} height={11} />Done</span>
            ) : (
              <button className="btn btn-primary" onClick={() => onJump?.(s.jumpTo)}>
                {s.cta}
                <Icons.Chevron width={12} height={12} style={{ opacity: 0.7 }} />
              </button>
            )}
          </div>
        ))}
      </div>

      <div className="card" style={{ marginTop: 20, padding: 0 }}>
        <div className="card-h">
          <div className="sec-h">Then…</div>
        </div>
        <div className="card-b">
          <div className="t2 fz12" style={{ lineHeight: 1.6 }}>
            Open a PR on an allowlisted repo. yaaof will create a ticket and the three review agents
            (architecture, security, style) will start working. You'll see them progress live on the
            ticket page, and three review comments will appear on the PR within a few minutes.
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Populated (metrics-first) ─────────────────────────────────────
function DashPopulated() {
  const data = window.YAAOF_DATA;
  const m = data.metrics;
  const liveTickets = data.tickets.filter((t) => t.status === 'review' && t.verdicts.arch !== 'skipped');

  return (
    <div className="page" style={{ maxWidth: 1440 }}>
      <div className="page-h">
        <div>
          <h1>Overview</h1>
          <div className="sub">acme · last 24h</div>
        </div>
        <div className="r g8">
          <span className="badge badge-soft">acme</span>
          <button className="btn">Last 24h <Icons.ChevronDown width={11} height={11} style={{ opacity: 0.65 }} /></button>
        </div>
      </div>

      {/* Metrics row */}
      <div className="r g12" style={{ marginBottom: 16 }}>
        <MetricTile label="Reviews 24h" value={m.reviews_24h} delta={`+${m.reviews_24h_delta}`} deltaTone="success" spark={m.spark_reviews_24h} />
        <MetricTile label="Avg latency" value={`${Math.floor(m.avg_latency_s/60)}m ${String(m.avg_latency_s%60).padStart(2,'0')}s`} delta={`${m.avg_latency_delta_s}s`} deltaTone="success" />
        <MetricTile label="Cost 24h" value={fmtCost(m.cost_24h)} delta={`+${fmtCost(m.cost_24h_delta).replace('$','$')}`} deltaTone="neutral" />
        <MetricTile label="Open tickets" value={m.open_tickets} sub="in review" />
        <MetricTile label="Queue · workers" value={`${m.queue_depth} · ${m.workers_active}/${m.workers_total}`} sub="0.0s wait p50" />
      </div>

      <div className="r g16 start" style={{ alignItems: 'stretch' }}>
        {/* Live agents */}
        <div className="card fl c" style={{ minHeight: 0 }}>
          <div className="card-h">
            <div className="sec-h">Live agents · in flight</div>
            <div className="fl" />
            <span className="r g6 t3 fz11"><span className="conn-dot" />updates live</span>
          </div>
          <div className="c">
            {liveTickets.map((t) => (
              <LiveTicketRow key={t.id} ticket={t} />
            ))}
          </div>
        </div>
        {/* Activity */}
        <div className="card c" style={{ width: 380, flex: 'none', minHeight: 0 }}>
          <div className="card-h">
            <div className="sec-h">Activity</div>
            <div className="fl" />
            <span className="t4 fz11 mono">last 24h</span>
          </div>
          <div className="c" style={{ padding: '4px 16px 14px' }}>
            {data.activity.map((a, i) => (
              <ActivityRow key={a.id} a={a} first={i === 0} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function MetricTile({ label, value, delta, deltaTone = 'neutral', sub, spark }) {
  const deltaColor =
    deltaTone === 'success' ? 'var(--success)' :
    deltaTone === 'danger'  ? 'var(--danger)'  : 'var(--text-3)';
  return (
    <div className="tile fl">
      <div className="r between">
        <div className="sec-h">{label}</div>
        {delta && <span className="mono fz11 tnum" style={{ color: deltaColor }}>{delta}</span>}
      </div>
      <div className="mono fw6 tnum" style={{ fontSize: 24, letterSpacing: '-0.02em' }}>{value}</div>
      {spark ? (
        <Sparkline points={spark} />
      ) : sub ? (
        <div className="t4 fz11 mono">{sub}</div>
      ) : (
        <div style={{ height: 14 }} />
      )}
    </div>
  );
}

function Sparkline({ points }) {
  const max = Math.max(...points, 1);
  const min = Math.min(...points);
  const w = 100, h = 22;
  const step = w / (points.length - 1);
  const norm = points.map((p, i) => [i * step, h - ((p - min) / (max - min || 1)) * h]);
  const path = norm.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const fillPath = path + ` L${w},${h} L0,${h} Z`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ width: '100%', height: 22 }}>
      <defs>
        <linearGradient id="spark-fill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%"  stopColor="var(--accent)" stopOpacity="0.4" />
          <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={fillPath} fill="url(#spark-fill)" />
      <path d={path} fill="none" stroke="var(--accent)" strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  );
}

function LiveTicketRow({ ticket }) {
  return (
    <Link to={`/tickets/${ticket.id}`} className="c g8" style={{ padding: '12px 16px', borderTop: '1px solid var(--border-soft)' }}>
      <div className="r g8">
        <span className="t4 mono fz11">#{ticket.number}</span>
        <span className="t3 mono fz11">{ticket.repo}</span>
        <span className="t1 fw5 fz12 ellip" style={{ minWidth: 0 }}>{ticket.title}</span>
        <div className="fl" />
        <span className="t4 fz11 mono"><UseAgo ts={ticket.updated} /></span>
      </div>
      <div className="r g14">
        {['arch', 'sec', 'style'].map((agent, i) => (
          <div className="r g6 fl" key={agent} style={{ minWidth: 0 }}>
            <span className="mono t4 fz11" style={{ width: 36 }}>{agent}</span>
            <AgentInlineState verdict={ticket.verdicts[agent]} />
          </div>
        ))}
      </div>
    </Link>
  );
}
function AgentInlineState({ verdict }) {
  if (verdict === 'running') return (
    <div className="r g6 fl">
      <span className="pulse-dot" />
      <div className="bar indeterminate fl" style={{ maxWidth: 80 }}><span className="bar-fill" /></div>
    </div>
  );
  if (verdict === 'queued') return (
    <div className="r g6 fl">
      <span style={{ width: 8, height: 8, borderRadius: 2, background: 'var(--surface-2)', border: '1px solid var(--border)' }} />
      <span className="t3 fz11">queued</span>
    </div>
  );
  if (verdict === 'APPROVED') return <span className="badge badge-success" style={{ height: 18 }}><span className="dot" />Approved</span>;
  if (verdict === 'CHANGES_REQUESTED') return <span className="badge badge-danger" style={{ height: 18 }}><span className="dot" />Changes</span>;
  if (verdict === 'COMMENT') return <span className="badge badge-soft" style={{ height: 18 }}><span className="dot" />Comment</span>;
  if (verdict === 'skipped') return <span className="t4 fz11">skipped</span>;
  return null;
}

function ActivityRow({ a, first }) {
  let icon, msg;
  switch (a.kind) {
    case 'review_posted':
      icon = <span style={{ width: 8, height: 8, borderRadius: 2, background:
        a.verdict === 'APPROVED' ? 'var(--success)' :
        a.verdict === 'CHANGES_REQUESTED' ? 'var(--danger)' :
        'var(--text-3)'
      }} />;
      msg = <><b className="t1 fw5">{a.agent}</b> {a.verdict === 'CHANGES_REQUESTED' ? 'requested changes' : a.verdict === 'APPROVED' ? 'approved' : 'commented'} on <span className="mono t2">#{a.pr}</span> · {a.repo}</>;
      break;
    case 'pr_opened':
      icon = <Icons.GitPR width={12} height={12} style={{ color: 'var(--accent)' }} />;
      msg = <><span className="mono t2">PR #{a.pr}</span> opened on {a.repo} · <span className="t3">{a.actor}</span></>;
      break;
    case 'pr_merged':
      icon = <Icons.CheckCircle width={12} height={12} style={{ color: 'var(--success)' }} />;
      msg = <><span className="mono t2">PR #{a.pr}</span> merged on {a.repo}</>;
      break;
    case 'lesson_added':
      icon = <Icons.Sparkle width={12} height={12} style={{ color: 'var(--accent)' }} />;
      msg = <><b className="t1 fw5">lesson</b> added on {a.repo} · "{a.lesson}"</>;
      break;
    default:
      icon = <span className="dot" />;
      msg = JSON.stringify(a);
  }
  return (
    <div className={"r g10 start " + (first ? 'flash-new' : '')} style={{ padding: '8px 0', borderTop: '1px solid var(--border-soft)' }}>
      <div style={{ width: 12, display: 'grid', placeItems: 'center', marginTop: 3 }}>{icon}</div>
      <div className="c g2 fl" style={{ minWidth: 0 }}>
        <div className="t2 fz12" style={{ lineHeight: 1.5 }}>{msg}</div>
        <div className="t4 mono fz11"><RelText ts={a.ts} /></div>
      </div>
    </div>
  );
}

Object.assign(window, { ScreenDashboard });
