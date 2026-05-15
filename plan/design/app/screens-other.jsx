// app/screens-other.jsx — Memory, Prompts, Repos, Settings

// ─── MEMORY ────────────────────────────────────────────────────────
function ScreenMemory({ route }) {
  const data = window.YAAOF_DATA;
  const [activeRepo, setActiveRepo] = useState(route.repo || 'r1');
  const [showNew, setShowNew] = useState(false);
  const toast = useToast();
  const repo = data.repos.find((r) => r.id === activeRepo) || data.repos[0];
  const lessons = data.lessons[repo.id] || [];

  return (
    <div className="page" style={{ maxWidth: 1080 }}>
      <div className="page-h">
        <div>
          <h1>Memory</h1>
          <div className="sub">Per-repo lessons applied to every review on that repo.</div>
        </div>
        <button className="btn btn-primary" onClick={() => setShowNew(true)}>
          <Icons.Plus width={13} height={13} />
          New lesson
        </button>
      </div>

      <div className="r g6 wrap" style={{ marginBottom: 16 }}>
        {data.repos.map((r) => {
          const isActive = r.id === activeRepo;
          const ct = (data.lessons[r.id] || []).length;
          return (
            <button
              key={r.id}
              onClick={() => setActiveRepo(r.id)}
              className={"badge " + (isActive ? 'badge-accent' : 'badge-soft')}
              style={{ cursor: 'default', height: 26, padding: '0 12px', fontSize: 12 }}
            >
              <span className="mono">{r.name}</span>
              <span className="mono" style={{ marginLeft: 6, opacity: 0.7 }}>{ct}</span>
            </button>
          );
        })}
      </div>

      <div className="t3 fz12" style={{ marginBottom: 14 }}>
        Lessons for <b className="t1 mono">{repo.name}</b> are added to the prompt for every review on this repo.
        See <code className="mono" style={{ background: 'var(--surface-2)', padding: '1px 5px', borderRadius: 4 }}>review_job.prompt_sent</code> entries in the audit log to verify.
      </div>

      {lessons.length === 0 ? (
        <div className="card">
          <div className="empty">
            <div className="title">No lessons for {repo.name} yet</div>
            <div>Lessons let you teach yaaof your team's preferences. Once you write one,<br/>every future review on this repo will apply it.</div>
            <div style={{ marginTop: 16 }}>
              <button className="btn btn-primary" onClick={() => setShowNew(true)}>
                <Icons.Plus width={13} height={13} />Write the first lesson
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="c g10">
          {lessons.map((l) => (
            <div key={l.id} className="card" style={{ padding: 14 }}>
              <div className="r g10 between" style={{ marginBottom: 8 }}>
                <div className="fw6 fz14">{l.title}</div>
                <div className="r g10 t4 fz11">
                  <span className="mono">from {l.source_pr}</span>
                  <span>·</span>
                  <span>added <UseAgo ts={l.created} /></span>
                  <span>·</span>
                  <span>applied <span className="mono tnum">{l.applied_count}×</span></span>
                  <button className="btn btn-ghost btn-icon-sm" title="Edit lesson">
                    <Icons.Edit width={13} height={13} />
                  </button>
                  <button className="btn btn-ghost btn-icon-sm" title="Delete lesson"
                    onClick={() => toast(`"${l.title}" deleted`, { action: { label: 'Undo', onClick: () => {} } })}>
                    <Icons.Trash width={13} height={13} />
                  </button>
                </div>
              </div>
              <div className="t2 fz12" style={{ lineHeight: 1.55 }}>{l.body}</div>
            </div>
          ))}
        </div>
      )}

      {showNew && <NewLessonModal repo={repo} onClose={() => setShowNew(false)} onSave={(title) => { toast(`Lesson "${title}" added to ${repo.name}`); setShowNew(false); }} />}
    </div>
  );
}

function NewLessonModal({ repo, onClose, onSave }) {
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const max = 1000;
  const left = max - body.length;
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="modal" style={{ width: 540 }}>
        <div className="card-h">
          <h3>New lesson</h3>
          <span className="t4 fz11">in <b className="t2 mono">{repo.name}</b></span>
          <div className="fl" />
          <button className="btn btn-ghost btn-icon-sm" onClick={onClose}><Icons.X width={14} height={14} /></button>
        </div>
        <div className="card-b c g12">
          <div className="c g6">
            <label className="sec-h">Title</label>
            <input
              className="input"
              placeholder="e.g. Don't suggest mocks in tests"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              autoFocus
            />
          </div>
          <div className="c g6">
            <div className="r between baseline">
              <label className="sec-h">Body</label>
              <span className={"t4 fz11 mono tnum " + (left < 100 ? 'verdict-changes' : '')}>{left} / {max}</span>
            </div>
            <textarea
              className="textarea"
              rows={8}
              maxLength={max}
              placeholder="Describe the rule and when it applies. Future reviews will read this verbatim."
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
          </div>
          <div className="r g8 end">
            <button className="btn" onClick={onClose}>Cancel</button>
            <button className="btn btn-primary" disabled={!title || !body} onClick={() => onSave(title)}>
              <Icons.Save width={13} height={13} />
              Save lesson
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

// ─── PROMPTS ───────────────────────────────────────────────────────
function ScreenPrompts({ route }) {
  const data = window.YAAOF_DATA;
  const [activeAgent, setActiveAgent] = useState(route.agent || 'arch');
  const [drafts, setDrafts] = useState(() => {
    const m = {};
    data.agents.forEach((a) => { m[a.id] = a.prompt; });
    return m;
  });
  const [savedDirty, setSavedDirty] = useState({});
  const toast = useToast();
  const agent = data.agents.find((a) => a.id === activeAgent) || data.agents[0];
  const value = drafts[activeAgent];
  const dirty = !!savedDirty[activeAgent];

  function onChange(v) {
    setDrafts({ ...drafts, [activeAgent]: v });
    setSavedDirty({ ...savedDirty, [activeAgent]: v !== agent.prompt });
  }

  function save() {
    toast(`${agent.name} prompt saved · applies to next review`);
    setSavedDirty({ ...savedDirty, [activeAgent]: false });
  }

  return (
    <div className="page" style={{ maxWidth: 1200 }}>
      <div className="page-h">
        <div>
          <h1>Prompts</h1>
          <div className="sub">3 built-in review agents · prompts editable · agent set is fixed in M01.</div>
        </div>
      </div>

      <div className="tabs">
        {data.agents.map((a) => (
          <button
            key={a.id}
            onClick={() => setActiveAgent(a.id)}
            className={"tab " + (a.id === activeAgent ? 'active' : '')}
          >
            {a.name}
            {savedDirty[a.id] && <span className="dot" style={{ background: 'var(--accent)', width: 6, height: 6, borderRadius: '50%', marginLeft: 2 }} />}
          </button>
        ))}
      </div>

      <div className="r between" style={{ marginBottom: 12 }}>
        <div className="r g8 t3 fz12">
          <span className="mono">p_{agent.id}_8c1a</span>
          <span>·</span>
          <span>updated 3d ago by you</span>
          <span>·</span>
          <span>applied to <span className="mono tnum t2">{agent.applied_to}</span> reviews</span>
        </div>
        <div className="r g8">
          <button className="btn" onClick={() => { if (confirm('Reset to default prompt?')) onChange(agent.prompt); }}>
            <Icons.Replay width={13} height={13} />
            Reset to default
          </button>
          <button className="btn btn-primary" disabled={!dirty} onClick={save}>
            <Icons.Save width={13} height={13} />
            Save
          </button>
        </div>
      </div>

      <div className="card" style={{ overflow: 'hidden' }}>
        <div className="card-h" style={{ paddingTop: 8, paddingBottom: 8, background: 'var(--bg-2)' }}>
          <span className="t3 mono fz11">prompt · markdown</span>
          <div className="fl" />
          <span className="t4 mono fz11 tnum">{value.length} chars · {value.split('\n').length} lines</span>
        </div>
        <textarea
          className="textarea mono"
          style={{
            border: 0,
            borderRadius: 0,
            minHeight: 480,
            background: 'var(--surface)',
            outline: 'none',
            boxShadow: 'none',
          }}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      </div>

      <div className="t4 fz11" style={{ marginTop: 10 }}>
        Saved prompts apply to the next review. In-flight reviews use the prompt snapshotted at job start
        (audit log shows the prompt hash).
      </div>
    </div>
  );
}

// ─── REPOS ─────────────────────────────────────────────────────────
function ScreenRepos() {
  const data = window.YAAOF_DATA;
  const [newRepo, setNewRepo] = useState('');
  const toast = useToast();
  const cols = 'minmax(220px, 1.6fr) 100px 160px 80px 120px 100px';

  return (
    <div className="page" style={{ maxWidth: 1080 }}>
      <div className="page-h">
        <div>
          <h1>Repos</h1>
          <div className="sub">Allowlist · yaaof opens a ticket when a PR lands on any of these.</div>
        </div>
      </div>

      <div className="card r g10" style={{ padding: '10px 14px', marginBottom: 16 }}>
        <Icons.Plus width={14} height={14} style={{ color: 'var(--text-3)' }} />
        <input
          className="input"
          style={{ flex: 1, maxWidth: 360, height: 28 }}
          placeholder="owner/name"
          value={newRepo}
          onChange={(e) => setNewRepo(e.target.value)}
        />
        <button className="btn">
          <Icons.GitHub width={13} height={13} />
          Verify access
        </button>
        <button className="btn btn-primary" disabled={!newRepo.includes('/')} onClick={() => { toast(`${newRepo} added to allowlist`); setNewRepo(''); }}>
          Add repo
        </button>
        <div className="fl" />
        <span className="t4 fz11">Repo must be accessible to the yaaof GitHub App.</span>
      </div>

      <div className="card" style={{ overflow: 'hidden' }}>
        <div className="thead" style={{ gridTemplateColumns: cols }}>
          <div>Repo</div>
          <div>Language</div>
          <div>Status</div>
          <div>Lessons</div>
          <div>Last review</div>
          <div></div>
        </div>
        {data.repos.map((r) => (
          <div key={r.id} className="trow" style={{ gridTemplateColumns: cols }}>
            <div className="r g8">
              <Icons.GitHub width={14} height={14} style={{ color: 'var(--text-3)' }} />
              <span className="mono fw6 fz13">{r.name}</span>
            </div>
            <div className="t3 fz12">{r.lang}</div>
            <div>
              {r.status === 'active' && <span className="badge badge-success"><span className="dot" />active</span>}
              {r.status === 'install-missing' && <span className="badge badge-danger"><span className="dot" />install missing</span>}
              {r.status === 'unreachable' && <span className="badge badge-danger"><span className="dot" />unreachable</span>}
            </div>
            <div>
              <Link to={`/memory`} className="mono fz12 tnum t2" style={{ borderBottom: '1px dashed var(--text-4)' }}>
                {r.lessons_count}
              </Link>
            </div>
            <div className="t3 fz12">
              {r.last_review_age_ms == null ? <span className="t4">never</span> : <span><UseAgo ts={Date.now() - r.last_review_age_ms} /></span>}
            </div>
            <div className="r g6 end">
              {r.status !== 'active' && <button className="btn btn-sm">Reconnect</button>}
              <button className="btn btn-ghost btn-icon-sm" title="More"><Icons.More width={14} height={14} /></button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── SETTINGS ──────────────────────────────────────────────────────
function ScreenSettings() {
  const data = window.YAAOF_DATA;
  const s = data.settings;
  const toast = useToast();
  return (
    <div className="page" style={{ maxWidth: 880 }}>
      <div className="page-h">
        <div>
          <h1>Settings</h1>
          <div className="sub">M01 has no auth. Single org · single self-hosted install.</div>
        </div>
      </div>

      <div className="c g14">
        {/* GitHub App */}
        <div className="card">
          <div className="card-h">
            <Icons.GitHub width={15} height={15} style={{ color: 'var(--text-2)' }} />
            <h3>GitHub App</h3>
            <div className="fl" />
            <span className="badge badge-success"><span className="dot" />installed</span>
          </div>
          <div className="card-b c g10">
            <div className="r g20 wrap t3 fz12">
              <span>org <b className="t1 mono">{s.github_app.org}</b></span>
              <span>install id <b className="t1 mono">{s.github_app.install_id}</b></span>
              <span>installed <b className="t1"><UseAgo ts={s.github_app.installed_on} /></b></span>
            </div>
            <div className="r g6">
              <a href={s.github_app.app_url} target="_blank" rel="noopener noreferrer" className="btn">
                <Icons.External width={13} height={13} />
                Manage on GitHub
              </a>
              <button className="btn">Reinstall</button>
            </div>
          </div>
        </div>

        {/* API key */}
        <div className="card">
          <div className="card-h">
            <Icons.Bolt width={15} height={15} style={{ color: 'var(--text-2)' }} />
            <h3>Model API key</h3>
            <div className="fl" />
            <span className="badge badge-success"><span className="dot" />configured</span>
          </div>
          <div className="card-b c g10">
            <div className="r g20 wrap t3 fz12">
              <span>provider <b className="t1">{s.api_key.provider}</b></span>
              <span>key <b className="t1 mono">{s.api_key.key_preview}</b></span>
              <span>added <b className="t1"><UseAgo ts={s.api_key.added} /></b></span>
            </div>
            <div className="r g6">
              <button className="btn">Rotate key</button>
              <button className="btn" onClick={() => toast('Connection OK · 412ms latency')}>
                Test connection
              </button>
            </div>
          </div>
        </div>

        {/* Plugin health */}
        <div className="card">
          <div className="card-h">
            <Icons.Live width={15} height={15} style={{ color: 'var(--text-2)' }} />
            <h3>Plugin health</h3>
            <div className="fl" />
            <span className="t4 fz11">refreshed <PluginRefresh /></span>
          </div>
          <div>
            {s.plugin_health.map((p, i) => (
              <div key={p.name} className="r g14" style={{ padding: '10px 16px', borderTop: i ? '1px solid var(--border-soft)' : 0 }}>
                <span className="mono fw6 fz13" style={{ width: 130 }}>{p.name}</span>
                <span className="badge badge-success"><span className="dot" />{p.status}</span>
                <div className="fl" />
                <span className="t3 mono fz11">
                  {p.latency_ms != null ? `${p.latency_ms}ms` : p.clients != null ? `${p.clients} clients` : '—'}
                </span>
                <span className="t4 mono fz11">checked <UseAgo ts={p.last_check} /></span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function PluginRefresh() {
  const now = useNow(1000);
  const t = Math.floor((now / 1000) % 10);
  return <span className="mono">{t}s ago</span>;
}

Object.assign(window, { ScreenMemory, ScreenPrompts, ScreenRepos, ScreenSettings });
