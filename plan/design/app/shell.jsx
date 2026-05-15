// app/shell.jsx — App shell: sidebar (pin/float), topbar, layout

const NAV = [
  { id: 'dashboard', path: '/dashboard', label: 'Dashboard', icon: 'Dashboard' },
  { id: 'tickets',   path: '/tickets',   label: 'Tickets',   icon: 'Tickets',  countKey: 'review' },
  { id: 'memory',    path: '/memory',    label: 'Memory',    icon: 'Brain' },
  { id: 'prompts',   path: '/prompts',   label: 'Prompts',   icon: 'Prompt' },
  { id: 'repos',     path: '/repos',     label: 'Repos',     icon: 'Repo',    countKey: 'repos' },
  { id: 'settings',  path: '/settings',  label: 'Settings',  icon: 'Settings' },
];

function SBLogo({ collapsed = false }) {
  return (
    <div className={"sb-brand " + (collapsed ? 'collapsed' : '')}>
      <div className="sb-logo">Y</div>
      {!collapsed && (
        <div className="sb-wordmark">
          <span className="name">yaaof</span>
          <span className="tag">logo · placeholder</span>
        </div>
      )}
    </div>
  );
}

function SidebarItem({ item, active, counts, onNav }) {
  const Ic = Icons[item.icon];
  const count = counts && item.countKey ? counts[item.countKey] : null;
  return (
    <Link to={item.path} className={"sb-item " + (active ? 'active' : '')} onClick={onNav}>
      <Ic className="ic" />
      <span className="lbl">{item.label}</span>
      {count != null && <span className={"count " + (item.countKey === 'review' && count > 0 ? 'live' : '')}>{count}</span>}
    </Link>
  );
}

function SidebarRailItem({ item, active, counts, onNav, onMouseEnter }) {
  const Ic = Icons[item.icon];
  const count = counts && item.countKey ? counts[item.countKey] : null;
  return (
    <Link to={item.path} className={"sb-rail-item " + (active ? 'active' : '')} onClick={onNav} onMouseEnter={onMouseEnter}>
      <Ic className="ic" />
      {count != null && count > 0 && item.countKey === 'review' && <span className="count-dot" />}
      <span className="sb-rail-tip">{item.label}{count != null ? ` · ${count}` : ''}</span>
    </Link>
  );
}

function SidebarPanel({ activeRoute, pinned, onPin, onNav, counts }) {
  return (
    <>
      <SBLogo />
      <nav className="sb-nav">
        <div className="sb-sec">Workspace</div>
        {NAV.map((n) => (
          <SidebarItem
            key={n.id}
            item={n}
            counts={counts}
            active={isActive(activeRoute, n)}
            onNav={onNav}
          />
        ))}
      </nav>
      <div className="sb-foot">
        <span className="ver-dot" />
        <span className="ver">v0.4.2</span>
        <button
          className={"pin-btn " + (pinned ? 'pinned' : '')}
          title={pinned ? 'Unpin (collapse to rail)' : 'Pin (always show)'}
          onClick={onPin}
        >
          {pinned ? <Icons.Pin width={14} height={14} /> : <Icons.PinOff width={14} height={14} />}
        </button>
      </div>
    </>
  );
}

function isActive(route, navItem) {
  if (route.name === 'ticket' && navItem.id === 'tickets') return true;
  return route.name === navItem.id;
}

function Sidebar({ route, pinned, onPin, panelOpen, setPanelOpen, counts, onNav }) {
  if (pinned) {
    return (
      <aside className="sb-shell" style={{ '--sidebar-w': '220px' }}>
        <SidebarPanel activeRoute={route} pinned={true} onPin={onPin} onNav={onNav} counts={counts} />
      </aside>
    );
  }
  // Floating: 48px rail; on hover, panel slides out
  return (
    <>
      <aside
        className="sb-shell"
        onMouseEnter={() => setPanelOpen(true)}
        onMouseLeave={(e) => {
          // close only when leaving the entire sidebar area (including panel)
          if (!panelOpen) return;
          const to = e.relatedTarget;
          if (to && to.closest && to.closest('.sb-float-panel')) return;
          setPanelOpen(false);
        }}
      >
        <SBLogo collapsed />
        <nav className="sb-nav collapsed">
          {NAV.map((n) => (
            <SidebarRailItem
              key={n.id}
              item={n}
              counts={counts}
              active={isActive(route, n)}
              onNav={() => { setPanelOpen(false); }}
              onMouseEnter={() => setPanelOpen(true)}
            />
          ))}
        </nav>
        <div className="sb-foot collapsed">
          <button className="pin-btn" title="Pin sidebar" onClick={onPin}>
            <Icons.PinOff width={14} height={14} />
          </button>
        </div>
      </aside>
      {panelOpen && (
        <div
          className="sb-float-panel"
          onMouseEnter={() => setPanelOpen(true)}
          onMouseLeave={() => setPanelOpen(false)}
        >
          <SidebarPanel
            activeRoute={route}
            pinned={false}
            onPin={() => { onPin(); setPanelOpen(false); }}
            onNav={() => setPanelOpen(false)}
            counts={counts}
          />
        </div>
      )}
    </>
  );
}

// ── Topbar ───────────────────────────────────────────────────────
function Topbar({ crumbs, right, onTheme, theme, onCommandK }) {
  return (
    <header className="topbar">
      <div className="crumbs">
        {crumbs.map((c, i) => {
          const inner = c.to
            ? <Link to={c.to} className={"crumb " + (c.active ? 'crumb-active' : '')}>{c.label}</Link>
            : <span className={"crumb " + (c.active ? 'crumb-active' : '')}>{c.label}</span>;
          return (
            <span key={i} className="r g6">
              {inner}
              {i < crumbs.length - 1 && <span className="crumb-sep">/</span>}
            </span>
          );
        })}
      </div>
      <div className="fl" />
      {right}
      <button className="btn btn-icon-sm" title="Command palette" onClick={onCommandK} style={{ marginRight: -4 }}>
        <Icons.Search width={14} height={14} />
      </button>
      <button className="btn btn-icon-sm" title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'} onClick={onTheme}>
        {theme === 'dark' ? <Icons.Sun width={14} height={14} /> : <Icons.Moon width={14} height={14} />}
      </button>
      <span className="connpill">
        <span className="conn-dot" />
        <span>live</span>
      </span>
    </header>
  );
}

// ── Crumbs builder ────────────────────────────────────────────────
function crumbsFor(route, ticket = null) {
  switch (route.name) {
    case 'dashboard': return [{ label: 'Dashboard', active: true }];
    case 'tickets':   return [{ label: 'Tickets', active: true }];
    case 'ticket':    return [
      { label: 'Tickets', to: '/tickets' },
      { label: ticket ? `#${ticket.number}` : route.id, active: true },
    ];
    case 'memory':    return [{ label: 'Memory', active: true }];
    case 'prompts':   return [{ label: 'Prompts', active: true }];
    case 'repos':     return [{ label: 'Repos', active: true }];
    case 'settings':  return [{ label: 'Settings', active: true }];
    default:          return [{ label: 'Dashboard', active: true }];
  }
}

Object.assign(window, { Sidebar, Topbar, NAV, crumbsFor });
