// app/app.jsx — yaaof main app

function App() {
  const [route, nav] = useRouter();
  const data = window.YAAOF_DATA;

  // Tweaks (persisted on host via __edit_mode_set_keys when present)
  const [t, setTweak] = useTweaks(window.TWEAKS_DEFAULTS);

  // Apply theme + density to <html>
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', t.theme || 'dark');
    document.documentElement.setAttribute('data-density', t.density || 'regular');
    // Accent override (only when not default)
    if (t.accent && t.accent !== 'violet') {
      const hue = { teal: 195, amber: 75, rose: 10, blue: 250 }[t.accent];
      if (hue != null) {
        document.documentElement.style.setProperty('--accent',     `oklch(0.72 0.19 ${hue})`);
        document.documentElement.style.setProperty('--accent-2',   `oklch(0.80 0.16 ${hue})`);
        document.documentElement.style.setProperty('--accent-dim', `oklch(0.46 0.14 ${hue})`);
        document.documentElement.style.setProperty('--accent-bg',  `oklch(0.30 0.10 ${hue} / 0.30)`);
        document.documentElement.style.setProperty('--accent-bg-2',`oklch(0.30 0.10 ${hue} / 0.14)`);
        document.documentElement.style.setProperty('--accent-border', `oklch(0.50 0.16 ${hue} / 0.55)`);
        return () => {
          document.documentElement.style.removeProperty('--accent');
          document.documentElement.style.removeProperty('--accent-2');
          document.documentElement.style.removeProperty('--accent-dim');
          document.documentElement.style.removeProperty('--accent-bg');
          document.documentElement.style.removeProperty('--accent-bg-2');
          document.documentElement.style.removeProperty('--accent-border');
        };
      }
    }
  }, [t.theme, t.density, t.accent]);

  // Sidebar pin/float
  const [panelOpen, setPanelOpen] = useState(false);
  const sidebarPinned = !!t.sidebarPinned;

  // Onboarding state for demo
  const onboarding = useMemo(() => ({
    github_app: !!t.onboardGithub,
    api_key:    !!t.onboardKey,
    repos:      !!t.onboardRepos,
  }), [t.onboardGithub, t.onboardKey, t.onboardRepos]);

  const counts = useMemo(() => ({
    review: data.tickets.filter((tk) => tk.status === 'review').length,
    repos:  data.repos.length,
  }), [data.tickets, data.repos]);

  const currentTicket = route.name === 'ticket'
    ? (data.tickets.find((tk) => tk.id === route.id) || data.tickets[0])
    : null;

  const crumbs = crumbsFor(route, currentTicket);

  // Per-route right-side topbar content
  const topRight = (
    <>
      {route.name === 'tickets' && (
        <span className="r g6 t3 fz11">
          <span className="conn-dot" />
          <span>updates live</span>
        </span>
      )}
    </>
  );

  // Page transition: simple key-based fade on the content
  const routeKey = route.name + (route.id || '') + (route.tab || '');

  return (
    <div className="app" data-sidebar={sidebarPinned ? 'pinned' : 'floating'} data-sidebar-open={panelOpen ? 'true' : 'false'}>
      <Sidebar
        route={route}
        pinned={sidebarPinned}
        onPin={() => setTweak('sidebarPinned', !sidebarPinned)}
        panelOpen={panelOpen}
        setPanelOpen={setPanelOpen}
        counts={counts}
        onNav={() => setPanelOpen(false)}
      />
      <div className="main">
        <Topbar
          crumbs={crumbs}
          right={topRight}
          theme={t.theme}
          onTheme={() => setTweak('theme', t.theme === 'dark' ? 'light' : 'dark')}
          onCommandK={() => {}}
        />
        <div className="content" key={routeKey}>
          {route.name === 'dashboard' && (
            <ScreenDashboard
              onboarding={onboarding}
              onJumpToSetup={(target) => {
                // For the demo, jumping to repos / settings just navigates;
                // in a real app this would also stage the onboarding checklist.
                window.location.hash = '#/' + target;
              }}
            />
          )}
          {route.name === 'tickets' && <ScreenTickets />}
          {route.name === 'ticket' && <ScreenTicket route={route} />}
          {route.name === 'memory' && <ScreenMemory route={route} />}
          {route.name === 'prompts' && <ScreenPrompts route={route} />}
          {route.name === 'repos' && <ScreenRepos />}
          {route.name === 'settings' && <ScreenSettings />}
        </div>
      </div>

      <YaaofTweaks t={t} setTweak={setTweak} />
    </div>
  );
}

// ─── Tweaks panel content ──────────────────────────────────────────
function YaaofTweaks({ t, setTweak }) {
  return (
    <TweaksPanel>
      <TweakSection label="Appearance" />
      <TweakRadio
        label="Theme"
        value={t.theme}
        options={['dark', 'light']}
        onChange={(v) => setTweak('theme', v)}
      />
      <TweakColor
        label="Accent"
        value={t.accent}
        options={[
          { value: 'violet', color: '#9b5af7' },
          { value: 'blue',   color: '#5a9bf7' },
          { value: 'teal',   color: '#1cc5b0' },
          { value: 'amber',  color: '#e5a93b' },
        ].map((o) => o.color)}
        onChange={(v) => {
          const map = { '#9b5af7': 'violet', '#5a9bf7': 'blue', '#1cc5b0': 'teal', '#e5a93b': 'amber' };
          setTweak('accent', map[v] || 'violet');
        }}
      />
      <TweakRadio
        label="Density"
        value={t.density}
        options={['compact', 'regular', 'comfy']}
        onChange={(v) => setTweak('density', v)}
      />

      <TweakSection label="Sidebar" />
      <TweakToggle
        label="Pin sidebar"
        value={!!t.sidebarPinned}
        onChange={(v) => setTweak('sidebarPinned', v)}
      />

      <TweakSection label="Onboarding demo" />
      <TweakToggle
        label="GitHub App installed"
        value={!!t.onboardGithub}
        onChange={(v) => setTweak('onboardGithub', v)}
      />
      <TweakToggle
        label="API key configured"
        value={!!t.onboardKey}
        onChange={(v) => setTweak('onboardKey', v)}
      />
      <TweakToggle
        label="Repo allowlisted"
        value={!!t.onboardRepos}
        onChange={(v) => setTweak('onboardRepos', v)}
      />
    </TweaksPanel>
  );
}

// ─── Bootstrap ─────────────────────────────────────────────────────
ReactDOM.createRoot(document.getElementById('root')).render(
  <ToastProvider>
    <App />
  </ToastProvider>
);
