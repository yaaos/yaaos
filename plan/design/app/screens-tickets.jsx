// app/screens-tickets.jsx — Tickets list (flat + grouped-by-status)

function FilterBar({ filter, setFilter, groupBy, setGroupBy, counts }) {
  return (
    <div className="r between wrap g12" style={{ marginBottom: 14 }}>
      <div className="r g6 wrap">
        <button
          className={"badge " + (filter.status === 'all' ? 'badge-soft' : 'badge-soft')}
          style={{ cursor: 'default', height: 22, ...(filter.status === 'all' ? { borderColor: 'var(--border-hard)', background: 'var(--surface-3)', color: 'var(--text)' } : {}) }}
          onClick={() => setFilter({ ...filter, status: 'all' })}
        >
          All <span className="t4 mono" style={{ marginLeft: 4 }}>{counts.all}</span>
        </button>
        <button
          className={"badge " + (filter.status === 'review' ? 'badge-accent' : 'badge-soft')}
          style={{ cursor: 'default', height: 22 }}
          onClick={() => setFilter({ ...filter, status: 'review' })}
        >
          <span className="dot" />Review <span className="mono" style={{ marginLeft: 4, opacity: 0.7 }}>{counts.review}</span>
        </button>
        <button
          className={"badge " + (filter.status === 'done' ? 'badge-success' : 'badge-soft')}
          style={{ cursor: 'default', height: 22 }}
          onClick={() => setFilter({ ...filter, status: 'done' })}
        >
          <span className="dot" />Done <span className="mono" style={{ marginLeft: 4, opacity: 0.7 }}>{counts.done}</span>
        </button>
        <div style={{ width: 8 }} />
        <button className="badge badge-soft" style={{ cursor: 'default', height: 22 }}>
          repo: all <Icons.ChevronDown width={10} height={10} style={{ opacity: 0.7 }} />
        </button>
        <button className="badge badge-soft" style={{ cursor: 'default', height: 22 }}>
          kind: all <Icons.ChevronDown width={10} height={10} style={{ opacity: 0.7 }} />
        </button>
        <button className="badge badge-soft" style={{ cursor: 'default', height: 22 }}>
          author: all <Icons.ChevronDown width={10} height={10} style={{ opacity: 0.7 }} />
        </button>
      </div>
      <div className="r g8">
        <span className="t4 mono" style={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '0.06em' }}>group</span>
        <div className="r" style={{ background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 6, padding: 2, gap: 2 }}>
          <button
            className="btn btn-sm"
            style={{
              height: 22, padding: '0 9px', border: 0,
              background: groupBy === 'none' ? 'var(--surface)' : 'transparent',
              boxShadow: groupBy === 'none' ? 'var(--shadow-sm)' : 'none',
            }}
            onClick={() => setGroupBy('none')}
          >None</button>
          <button
            className="btn btn-sm"
            style={{
              height: 22, padding: '0 9px', border: 0,
              background: groupBy === 'status' ? 'var(--surface)' : 'transparent',
              boxShadow: groupBy === 'status' ? 'var(--shadow-sm)' : 'none',
            }}
            onClick={() => setGroupBy('status')}
          >Status</button>
        </div>
      </div>
    </div>
  );
}

// Column template — title flexes
const TIX_COLS = '78px 1.7fr 110px 88px 70px 28px 130px 60px 64px';
const TIX_COLS_GROUPED = '1.7fr 110px 88px 70px 28px 130px 60px 64px';

function TicketRow({ t, grouped = false, isLive = false }) {
  const tokenDisplay = isLive ? <LiveTokens base={t.tokens_total} /> : null;
  return (
    <Link
      to={`/tickets/${t.id}`}
      className="trow"
      style={{ gridTemplateColumns: grouped ? TIX_COLS_GROUPED : TIX_COLS, ...(isLive ? {} : {}) }}
    >
      {!grouped && <div><StatusBadge status={t.status} /></div>}
      <div className="c g4" style={{ minWidth: 0 }}>
        <div className="r g8" style={{ minWidth: 0 }}>
          <span className="t4 mono fz11">#{t.number}</span>
          <span className="t3 mono fz11">{t.repo}</span>
          {t.skip_reason && <span className="chip" style={{ color: 'var(--text-4)' }}>skip · {t.skip_reason}</span>}
        </div>
        <div className="t1 fw5 ellip" style={{ fontSize: 13 }}>{t.title}</div>
      </div>
      <div><KindChip kind={t.kind} /></div>
      <div className="r g8">
        <VerdictDots v={[t.verdicts.arch, t.verdicts.sec, t.verdicts.style]} />
        {isLive && <span className="pulse-dot" style={{ width: 6, height: 6 }} />}
      </div>
      <div className="r g6 t3 fz11">
        <Icons.Coin width={12} height={12} style={{ opacity: 0.6 }} />
        <span className="mono tnum">{fmtCost(t.cost_usd)}</span>
      </div>
      <div><SourceIcon source={t.source} size={16} /></div>
      <div className="r g6">
        <Avatar name={t.actor} size={18} />
        <span className="t2 fz11 ellip">{t.actor}</span>
      </div>
      <div className="r g6 t3 fz11">
        <Icons.Token width={12} height={12} style={{ opacity: 0.55 }} />
        {tokenDisplay || <span className="mono tnum">{fmtTokens(t.tokens_total)}</span>}
      </div>
      <div className="t4 mono fz11">
        <UseAgo ts={t.updated} />
      </div>
    </Link>
  );
}

// Live token counter for the in-flight ticket
function LiveTokens({ base }) {
  const now = useNow(700);
  // tick up gently
  const extra = Math.floor((now % 60_000) / 700) * 4;
  return <span className="mono tnum" style={{ color: 'var(--accent)' }}>{fmtTokens((base || 0) + extra)}</span>;
}

function TicketHead({ grouped = false }) {
  return (
    <div className="thead" style={{ gridTemplateColumns: grouped ? TIX_COLS_GROUPED : TIX_COLS }}>
      {!grouped && <div>Status</div>}
      <div>Ticket</div>
      <div>Kind</div>
      <div>Review</div>
      <div>Cost</div>
      <div>Src</div>
      <div>Author</div>
      <div>Tokens</div>
      <div>Updated</div>
    </div>
  );
}

function ScreenTickets() {
  const data = window.YAAOF_DATA;
  const [filter, setFilter] = useState({ status: 'all' });
  const [groupBy, setGroupBy] = useState('none');
  const [search, setSearch] = useState('');

  const all = data.tickets;
  const filtered = useMemo(() => {
    let xs = all;
    if (filter.status !== 'all') xs = xs.filter((t) => t.status === filter.status);
    if (search) {
      const q = search.toLowerCase();
      xs = xs.filter((t) =>
        t.title.toLowerCase().includes(q) ||
        t.repo.toLowerCase().includes(q) ||
        ('#' + t.number).includes(q) ||
        t.actor.toLowerCase().includes(q)
      );
    }
    return xs;
  }, [all, filter, search]);

  const counts = useMemo(() => ({
    all:    all.length,
    review: all.filter((t) => t.status === 'review').length,
    done:   all.filter((t) => t.status === 'done').length,
  }), [all]);

  // Groupings (only used in grouped mode)
  const grouped = useMemo(() => {
    if (groupBy !== 'status') return null;
    const review = filtered.filter((t) => t.status === 'review');
    const done   = filtered.filter((t) => t.status === 'done');
    const groups = [];
    if (filter.status === 'all' || filter.status === 'review')
      groups.push({ key: 'review', label: 'Review', tone: 'accent', items: review });
    if (filter.status === 'all' || filter.status === 'done')
      groups.push({ key: 'done', label: 'Done', tone: 'success', items: done });
    return groups;
  }, [filtered, groupBy, filter.status]);

  return (
    <div className="page" style={{ maxWidth: 1500 }}>
      <div className="page-h">
        <div>
          <h1>Tickets</h1>
          <div className="sub">acme · {counts.review} in review · {counts.done} done</div>
        </div>
        <div className="r g10">
          <div className="search" style={{ width: 280 }}>
            <Icons.Search width={13} height={13} />
            <input placeholder="Filter tickets…" value={search} onChange={(e) => setSearch(e.target.value)} />
            <span className="kbd">/</span>
          </div>
          <button className="btn">
            <Icons.Filter width={13} height={13} />
            Sort · newest
            <Icons.ChevronDown width={11} height={11} style={{ opacity: 0.65 }} />
          </button>
        </div>
      </div>

      <FilterBar filter={filter} setFilter={setFilter} groupBy={groupBy} setGroupBy={setGroupBy} counts={counts} />

      {groupBy === 'none' ? (
        <div className="card" style={{ overflow: 'hidden' }}>
          <TicketHead />
          <div>
            {filtered.length === 0 ? (
              <div className="empty">
                <div className="title">No tickets match these filters</div>
                <div>Try clearing filters or adjusting your search.</div>
              </div>
            ) : (
              filtered.map((t) => <TicketRow key={t.id} t={t} isLive={!!t.is_live} />)
            )}
          </div>
        </div>
      ) : (
        <div className="c g16">
          {grouped.map((g) => (
            <div key={g.key} className="c g6">
              <div className="r g8 baseline" style={{ marginBottom: 2 }}>
                <StatusBadge status={g.key} />
                <span className="t3 mono fz11">{g.items.length}</span>
                <div className="fl" />
                {g.key === 'review' && <span className="r g6 t3 fz11"><span className="conn-dot" />updates live</span>}
              </div>
              <div className="card" style={{ overflow: 'hidden' }}>
                <TicketHead grouped />
                {g.items.length === 0 ? (
                  <div className="empty" style={{ padding: '28px 16px' }}>
                    <div className="t3 fz12">No tickets in {g.label.toLowerCase()}</div>
                  </div>
                ) : (
                  g.items.map((t) => <TicketRow key={t.id} t={t} grouped isLive={!!t.is_live} />)
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

Object.assign(window, { ScreenTickets });
