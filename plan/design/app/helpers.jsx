// app/helpers.jsx — shared utilities used across screens

const { useState, useEffect, useRef, useMemo, useCallback, useLayoutEffect, Fragment } = React;

// Time formatting ────────────────────────────────────────────────────
function relTime(ts, now = Date.now()) {
  const d = Math.max(0, now - ts);
  if (d < 5_000) return 'just now';
  if (d < 60_000) return Math.floor(d / 1000) + 's ago';
  if (d < 3_600_000) return Math.floor(d / 60_000) + 'm ago';
  if (d < 86_400_000) return Math.floor(d / 3_600_000) + 'h ago';
  return Math.floor(d / 86_400_000) + 'd ago';
}
function relShort(ts, now = Date.now()) {
  const d = Math.max(0, now - ts);
  if (d < 60_000) return Math.floor(d / 1000) + 's';
  if (d < 3_600_000) return Math.floor(d / 60_000) + 'm';
  if (d < 86_400_000) return Math.floor(d / 3_600_000) + 'h';
  return Math.floor(d / 86_400_000) + 'd';
}
function durationMs(ms) {
  if (ms == null) return '—';
  const s = Math.round(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}m ${r.toString().padStart(2, '0')}s`;
}
function fmtCost(usd) {
  if (usd == null) return '—';
  if (usd === 0) return '$0.00';
  if (usd < 0.01) return '<$0.01';
  return '$' + usd.toFixed(2);
}
function fmtTokens(n) {
  if (n == null) return '—';
  if (n === 0) return '0';
  if (n < 1000) return String(n);
  if (n < 100_000) return (n / 1000).toFixed(1) + 'k';
  return Math.round(n / 1000) + 'k';
}

// ── Tick hook: re-render every N ms so relative times stay fresh ──
function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

// ── Hash-based router ──────────────────────────────────────────────
// Routes:
//   #/dashboard
//   #/tickets
//   #/tickets/:id
//   #/tickets/:id/audit
//   #/memory
//   #/prompts
//   #/repos
//   #/settings
function parseRoute(hash) {
  const raw = (hash || '').replace(/^#\/?/, '');
  if (!raw) return { name: 'dashboard' };
  const [head, ...rest] = raw.split('/');
  if (head === 'tickets') {
    if (rest.length === 0) return { name: 'tickets' };
    if (rest.length === 1) return { name: 'ticket', id: rest[0], tab: 'review' };
    return { name: 'ticket', id: rest[0], tab: rest[1] || 'review' };
  }
  if (head === 'dashboard') return { name: 'dashboard' };
  if (head === 'memory')    return { name: 'memory', repo: rest[0] || null };
  if (head === 'prompts')   return { name: 'prompts', agent: rest[0] || null };
  if (head === 'repos')     return { name: 'repos' };
  if (head === 'settings')  return { name: 'settings' };
  return { name: 'dashboard' };
}
function useRouter() {
  const [route, setRoute] = useState(parseRoute(window.location.hash));
  useEffect(() => {
    const onHash = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const nav = useCallback((path) => {
    window.location.hash = path.startsWith('#') ? path : '#' + path;
  }, []);
  return [route, nav];
}
function Link({ to, children, className = '', style, onClick }) {
  const handle = useCallback((e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    window.location.hash = to.startsWith('#') ? to : '#' + to;
    if (onClick) onClick(e);
  }, [to, onClick]);
  return <a href={'#' + to.replace(/^#/, '')} className={className} style={style} onClick={handle}>{children}</a>;
}

// ── Avatar ────────────────────────────────────────────────────────
function Avatar({ name, kind = 'user', size = 18 }) {
  if (kind === 'system') {
    return <div className="av sys" style={{ width: size, height: size }}>•</div>;
  }
  if (kind === 'agent') {
    return <div className="av agent" style={{ width: size, height: size }}>{(name || '?').slice(0,1).toUpperCase()}</div>;
  }
  // user — initials
  const initials = (name || '?').split(/[-_\s]/).slice(0, 2).map(s => s[0]).join('').toUpperCase();
  return <div className="av" style={{ width: size, height: size }}>{initials}</div>;
}

// ── Verdict + status chips ────────────────────────────────────────
function VerdictBadge({ verdict, small = false }) {
  const cls = small ? 'badge badge-mono' : 'badge';
  if (verdict === 'APPROVED')          return <span className={cls + ' badge-success'}><span className="dot" />Approved</span>;
  if (verdict === 'CHANGES_REQUESTED') return <span className={cls + ' badge-danger'}><span className="dot" />Changes</span>;
  if (verdict === 'COMMENT')           return <span className={cls + ' badge-soft'}><span className="dot" />Comment</span>;
  if (verdict === 'running')           return <span className={cls + ' badge-accent'}><span className="pulse-dot" style={{ width: 6, height: 6 }} />Running</span>;
  if (verdict === 'queued')            return <span className={cls + ' badge-soft'}><span className="dot" style={{ background: 'var(--text-3)' }} />Queued</span>;
  if (verdict === 'skipped')           return <span className={cls + ' badge-soft'} style={{ color: 'var(--text-4)' }}><span className="dot" />Skipped</span>;
  return <span className={cls + ' badge-soft'}>{verdict}</span>;
}

function StatusBadge({ status }) {
  if (status === 'review') return <span className="badge badge-accent"><span className="dot" />Review</span>;
  if (status === 'done')   return <span className="badge badge-success"><span className="dot" />Done</span>;
  return <span className="badge badge-soft">{status}</span>;
}

function KindChip({ kind = 'new feature' }) {
  return (
    <span className="chip nowrap" style={{ textTransform: 'lowercase', whiteSpace: 'nowrap' }}>
      <span className="dot" style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--text-3)' }} />
      {kind}
    </span>
  );
}

function SourceIcon({ source, size = 16 }) {
  const s = source || 'github_pr';
  const label = s === 'github_pr' ? 'PR' : s === 'linear' ? 'L' : s === 'slack' ? 'S' : s === 'ops_alert' ? '!' : '?';
  return (
    <span
      title={s}
      style={{
        display: 'inline-grid', placeItems: 'center',
        width: size, height: size,
        border: '1px solid var(--border)',
        borderRadius: 4,
        background: 'var(--surface-2)',
        fontFamily: 'Geist Mono, monospace',
        fontSize: size <= 14 ? 8 : 9,
        fontWeight: 700,
        color: 'var(--text-3)',
        flex: 'none',
        letterSpacing: 0,
      }}
    >{label}</span>
  );
}

// ── Verdict dots (used in lists & dashboard) ──────────────────────
function VerdictDots({ v = [] }) {
  return (
    <span className="r g4" style={{ display: 'inline-flex' }}>
      {v.map((vv, j) => {
        const key = j;
        if (vv === 'APPROVED')          return <span key={key} title="approved" style={{ width: 9, height: 9, borderRadius: 2, background: 'var(--success)' }} />;
        if (vv === 'CHANGES_REQUESTED') return <span key={key} title="changes requested" style={{ width: 9, height: 9, borderRadius: 2, background: 'var(--danger)' }} />;
        if (vv === 'COMMENT')           return <span key={key} title="comment" style={{ width: 9, height: 9, borderRadius: 2, background: 'var(--text-3)' }} />;
        if (vv === 'running')           return <span key={key} className="pulse-dot" title="running" style={{ width: 9, height: 9, borderRadius: 2 }} />;
        if (vv === 'queued')            return <span key={key} title="queued" style={{ width: 9, height: 9, borderRadius: 2, background: 'var(--surface-2)', border: '1px solid var(--border)' }} />;
        return <span key={key} title="skipped" style={{ width: 9, height: 9, borderRadius: 2, background: 'transparent', border: '1px dashed var(--text-4)' }} />;
      })}
    </span>
  );
}

// ── Sev dot ───────────────────────────────────────────────────────
function SevDot({ sev }) {
  const cls = sev === 'must-fix' ? 'sev-must' : sev === 'nit' ? 'sev-nit' : sev === 'suggestion' ? 'sev-sug' : 'sev-info';
  return <span className={'sev-dot ' + cls} />;
}

// ── Toast manager ─────────────────────────────────────────────────
const ToastCtx = React.createContext({ toast: () => {} });
function ToastProvider({ children }) {
  const [list, setList] = useState([]);
  const idRef = useRef(0);
  const toast = useCallback((msg, opts = {}) => {
    const id = ++idRef.current;
    setList((l) => [...l, { id, msg, ...opts }]);
    setTimeout(() => setList((l) => l.filter((t) => t.id !== id)), opts.ttl || 3200);
  }, []);
  return (
    <ToastCtx.Provider value={{ toast }}>
      {children}
      <div className="toast-stack">
        {list.map((t) => (
          <div key={t.id} className="toast">
            {t.icon !== false && <Icons.CheckCircle width={16} height={16} style={{ color: 'var(--accent)', flex: 'none' }} />}
            <div className="fl">{t.msg}</div>
            {t.action && <button className="btn btn-ghost btn-sm" onClick={t.action.onClick}>{t.action.label}</button>}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
function useToast() {
  return React.useContext(ToastCtx).toast;
}

// ── Tooltip (very small) ──────────────────────────────────────────
function Tip({ tip, children, ...rest }) {
  return <span title={tip} {...rest}>{children}</span>;
}

// ── Source line component (used in ticket detail) ─────────────────
function SourceLine({ ticket }) {
  const pr = ticket.pr;
  return (
    <div className="r g8" style={{ fontSize: 12, color: 'var(--text-3)', flexWrap: 'wrap', rowGap: 6 }}>
      <span className="sec-h" style={{ flex: 'none' }}>Source</span>
      <SourceIcon source={ticket.source} />
      <span className="t2 nowrap">
        <b className="t1 mono">{ticket.source === 'github_pr' ? `PR #${pr.number}` : ticket.source}</b>{' '}
        on <b className="t1 mono">{ticket.repo}</b>
      </span>
      <span className="t4">·</span>
      <span className="r g6 nowrap"><Avatar name={pr.author} size={14} />{pr.author}</span>
      <span className="t4">·</span>
      <span className="nowrap">{ticket.status === 'done' ? 'merged' : 'opened'} <UseAgo ts={ticket.created} /></span>
      <span className="t4">·</span>
      <span className="mono t2 nowrap">{pr.head} → {pr.base}</span>
      <span className="t4">·</span>
      <span className="mono nowrap"><span style={{ color: 'var(--success)' }}>+{pr.additions}</span>{' '}
        <span style={{ color: 'var(--danger)' }}>−{pr.deletions}</span>{' '}
        <span className="t4">in {pr.files} files</span></span>
      <span className="t4">·</span>
      <a href={pr.html_url} target="_blank" rel="noopener noreferrer" className="r g4 nowrap" style={{ color: 'var(--text-2)', borderBottom: '1px dashed var(--text-4)' }}>
        open in GitHub <Icons.External width={11} height={11} />
      </a>
    </div>
  );
}

function UseAgo({ ts }) {
  const now = useNow(15_000);
  return <span>{relTime(ts, now)}</span>;
}

// ── Format helpers exposed for screens ──────────────────────────
Object.assign(window, {
  relTime, relShort, durationMs, fmtCost, fmtTokens,
  useNow, useRouter, parseRoute, Link,
  Avatar, VerdictBadge, StatusBadge, KindChip, SourceIcon, VerdictDots, SevDot,
  ToastProvider, useToast, Tip, SourceLine, UseAgo,
});
