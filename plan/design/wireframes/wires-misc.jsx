// wires-misc.jsx — Memory, Prompts, Repos, Settings (single layout each)

function Memory() {
  const lessons = [
    { t: "Don't suggest mocks in tests", b: 'Our test pattern is to use lightweight factories from test/factories/, not mocks. When you see jest.mock(), suggest the factory equivalent.', pr: '#2418', age: '8d', count: 217 },
    { t: 'Use @/lib/queries for server state', b: 'Never inline fetch() or raw useEffect-based fetching. We standardized on TanStack Query wrappers.', pr: '#2401', age: '14d', count: 412 },
    { t: 'Storybook stories are mandatory for shadcn variants', b: 'Any new variant of a shadcn primitive needs a Storybook story showing all states.', pr: '#2390', age: '22d', count: 188 },
    { t: 'No new global Zustand slices', b: 'Consolidating on URL state and TanStack Query for server state. Push back on new Zustand slices.', pr: '#2377', age: '30d', count: 76 },
  ];
  return (
    <WFShell active="mem" crumbs={[{ label: 'Memory', active: true }]}>
      <WFNote tag="MEMORY" title="Repo selector top, lessons list, body up to 1000 chars">
        Engineers write lessons; agents apply them. Body field has a live char counter.
        Lessons appear in the audit log via review_job.prompt_sent.lessons_count.
      </WFNote>
      <div className="wf-page-h">
        <h1 className="wf-h1">Memory</h1>
        <button className="wf-btn wf-btn-primary">+ New lesson</button>
      </div>
      <div className="r g6">
        <span className="wf-chip acc">acme/web · 4</span>
        <span className="wf-chip">acme/api · 3</span>
        <span className="wf-chip">acme/infra · 0</span>
        <span className="wf-chip">acme/mobile · 1</span>
        <span className="wf-chip">acme/ml-pipeline · 0</span>
      </div>
      <div className="wf-tx-3" style={{ fontSize: 11 }}>
        Lessons for <b className="wf-tx" style={{ fontFamily: 'JetBrains Mono, monospace' }}>acme/web</b> are added to the prompt for every review on this repo.
      </div>
      <div className="c g8 fl" style={{ minHeight: 0, overflow: 'hidden' }}>
        {lessons.map((l, i) => (
          <div key={i} className="wf-block c g8" style={{ padding: 12 }}>
            <div className="r between">
              <div className="wf-tx" style={{ fontWeight: 600 }}>{l.t}</div>
              <div className="r g8 wf-tx-3" style={{ fontSize: 10.5 }}>
                <span className="wf-tx-mono">from {l.pr}</span>
                <span>·</span>
                <span>added {l.age} ago</span>
                <span>·</span>
                <span>applied {l.count}×</span>
                <button className="wf-btn-ghost wf-btn" style={{ fontSize: 10 }}>Edit</button>
                <button className="wf-btn-ghost wf-btn" style={{ fontSize: 10, color: 'var(--wf-danger)' }}>Delete</button>
              </div>
            </div>
            <div className="wf-tx-2" style={{ fontSize: 11.5 }}>{l.b}</div>
          </div>
        ))}
      </div>
    </WFShell>
  );
}

function Prompts() {
  return (
    <WFShell active="pmt" crumbs={[{ label: 'Prompts', active: true }]}>
      <WFNote tag="PROMPTS" title="Tabbed per agent, single big monospaced textarea, Reset to default">
        Edits apply to the next review. In-flight reviews use the snapshotted prompt
        captured at job start (audit log shows the prompt hash).
      </WFNote>
      <div className="wf-page-h">
        <div>
          <h1 className="wf-h1">Prompts</h1>
          <div className="wf-sub">3 built-in review agents · prompts editable; agent set is fixed in M01.</div>
        </div>
      </div>
      <div className="wf-tabs">
        <div className="wf-tab act">Architecture</div>
        <div className="wf-tab">Security</div>
        <div className="wf-tab">Style</div>
      </div>
      <div className="r between">
        <div className="r g8 wf-tx-3" style={{ fontSize: 11 }}>
          <span className="wf-tx-mono">hash p_arch_8c1a</span>
          <span>·</span>
          <span>updated 3d ago by you</span>
          <span>·</span>
          <span>applied to 412 reviews</span>
        </div>
        <div className="r g8">
          <button className="wf-btn">Reset to default…</button>
          <button className="wf-btn wf-btn-primary">Save</button>
        </div>
      </div>
      <div className="fl c">
        <div className="wf-box-soft c fl" style={{ overflow: 'hidden' }}>
          <div className="r between" style={{ padding: '6px 10px', borderBottom: '1px solid var(--wf-stroke-soft)', background: 'var(--wf-fill-2)' }}>
            <div className="wf-tx-3 wf-tx-mono" style={{ fontSize: 10 }}>prompt · markdown · monospaced editor</div>
            <div className="wf-tx-3 wf-tx-mono" style={{ fontSize: 10 }}>418 / 8000 chars · 92 lines</div>
          </div>
          <div className="wf-code fl" style={{ border: 0, borderRadius: 0, padding: 14, fontSize: 11, lineHeight: 1.7 }}>{`You are a senior staff engineer reviewing a pull request for architectural soundness.

Focus on:
- Module boundaries and dependency direction
- Coupling and cohesion of new code with existing surfaces
- Whether new abstractions earn their complexity
- Long-term maintainability and extension points
- Data ownership and state placement

Avoid:
- Style nits (the style agent handles those)
- Security findings (the security agent handles those)

Output your verdict as APPROVED, CHANGES_REQUESTED, or COMMENT.
Provide structured findings: file:line, severity, title, body.

Apply the lessons listed below before drafting your review.`}</div>
        </div>
      </div>
    </WFShell>
  );
}

function Repos() {
  const rows = [
    { name: 'acme/web',         lang: 'TypeScript', status: 'active',           lessons: 4, last: '4m ago' },
    { name: 'acme/api',         lang: 'Python',     status: 'active',           lessons: 3, last: '12m ago' },
    { name: 'acme/mobile',      lang: 'Swift',      status: 'active',           lessons: 1, last: '3h ago' },
    { name: 'acme/infra',       lang: 'HCL',        status: 'install-missing',  lessons: 0, last: 'never' },
    { name: 'acme/ml-pipeline', lang: 'Python',     status: 'unreachable',      lessons: 0, last: '2d ago' },
  ];
  return (
    <WFShell active="rep" crumbs={[{ label: 'Repos', active: true }]}>
      <WFNote tag="REPOS" title="Allowlist with per-repo status; add-form inline">
        Status badge is the at-a-glance health check. install-missing and
        unreachable both need a 'reconnect' action; loud red dot.
      </WFNote>
      <div className="wf-page-h">
        <h1 className="wf-h1">Repos</h1>
        <button className="wf-btn wf-btn-primary">+ Add repo</button>
      </div>
      <div className="wf-block r g8" style={{ padding: 10 }}>
        <span className="wf-tx-3" style={{ fontSize: 11 }}>Add a repo to the allowlist</span>
        <div className="wf-input placeholder fl" style={{ maxWidth: 280 }}>owner/name</div>
        <button className="wf-btn">Verify access</button>
      </div>
      <div className="wf-box fl c" style={{ overflow: 'hidden', minHeight: 0 }}>
        <div className="wf-thead" style={{ gridTemplateColumns: '1fr 100px 140px 80px 100px 100px' }}>
          <div>repo</div><div>language</div><div>status</div><div>lessons</div><div>last review</div><div></div>
        </div>
        {rows.map((r, i) => (
          <div key={i} className="wf-trow" style={{ gridTemplateColumns: '1fr 100px 140px 80px 100px 100px' }}>
            <div className="wf-tx wf-tx-mono" style={{ fontWeight: 600 }}>{r.name}</div>
            <div className="wf-tx-3" style={{ fontSize: 11 }}>{r.lang}</div>
            <div>
              {r.status === 'active'           && <span className="wf-chip ok"><span className="dot" />active</span>}
              {r.status === 'install-missing'  && <span className="wf-chip bad"><span className="dot" />install missing</span>}
              {r.status === 'unreachable'      && <span className="wf-chip bad"><span className="dot" />unreachable</span>}
            </div>
            <div className="wf-tx-3 wf-tx-mono" style={{ fontSize: 11 }}>{r.lessons}</div>
            <div className="wf-tx-3" style={{ fontSize: 11 }}>{r.last}</div>
            <div className="r g6 end">
              {r.status !== 'active' && <button className="wf-btn">Reconnect</button>}
              <button className="wf-btn-ghost wf-btn" style={{ fontSize: 10 }}>…</button>
            </div>
          </div>
        ))}
      </div>
    </WFShell>
  );
}

function Settings() {
  return (
    <WFShell active="set" crumbs={[{ label: 'Settings', active: true }]}>
      <WFNote tag="SETTINGS" title="App install · API key · plugin health">
        No auth in M01 — no user / org / permissions UI. Plugin health is the only
        moving piece on this page.
      </WFNote>
      <div className="wf-page-h">
        <h1 className="wf-h1">Settings</h1>
      </div>
      <div className="c g12 fl">
        <div className="wf-block c g10">
          <div className="r between">
            <div className="wf-sec-h">GitHub App</div>
            <span className="wf-chip ok"><span className="dot" />installed</span>
          </div>
          <div className="r g16 wf-tx-3" style={{ fontSize: 11 }}>
            <span>org <b className="wf-tx wf-tx-mono">acme</b></span>
            <span>install id <b className="wf-tx wf-tx-mono">4488213</b></span>
            <span>installed <b className="wf-tx">41 days ago</b></span>
          </div>
          <div className="r g8">
            <button className="wf-btn">Manage on GitHub ↗</button>
            <button className="wf-btn">Reinstall</button>
          </div>
        </div>
        <div className="wf-block c g10">
          <div className="r between">
            <div className="wf-sec-h">Model API key</div>
            <span className="wf-chip ok"><span className="dot" />configured</span>
          </div>
          <div className="r g16 wf-tx-3" style={{ fontSize: 11 }}>
            <span>provider <b className="wf-tx">anthropic</b></span>
            <span>key <b className="wf-tx wf-tx-mono">sk-ant-…GqL7</b></span>
            <span>added <b className="wf-tx">41 days ago</b></span>
          </div>
          <div className="r g8">
            <button className="wf-btn">Rotate key</button>
            <button className="wf-btn">Test connection</button>
          </div>
        </div>
        <div className="wf-block c g6">
          <div className="r between">
            <div className="wf-sec-h">Plugin health</div>
            <span className="wf-tx-3" style={{ fontSize: 10 }}>refreshed 4s ago</span>
          </div>
          <div className="c">
            {[
              { n: 'github',       lat: '78ms',  s: 'healthy' },
              { n: 'anthropic',    lat: '412ms', s: 'healthy' },
              { n: 'claude-code',  lat: '—',     s: 'healthy' },
              { n: 'sse',          lat: '3 clients', s: 'healthy' },
            ].map((p, i) => (
              <div key={i} className="r g12" style={{ padding: '8px 0', borderTop: i ? '1px solid var(--wf-stroke-soft)' : 0 }}>
                <span className="wf-tx-mono" style={{ width: 110, fontWeight: 600 }}>{p.n}</span>
                <span className="wf-chip ok"><span className="dot" />{p.s}</span>
                <span className="fl" />
                <span className="wf-tx-3 wf-tx-mono" style={{ fontSize: 10.5 }}>{p.lat}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </WFShell>
  );
}

Object.assign(window, { Memory, Prompts, Repos, Settings });
