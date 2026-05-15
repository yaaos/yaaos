// wires-app.jsx — design canvas composition

const ARTBOARD_W = 1280;
const ARTBOARD_H = 820;

function WireApp() {
  return (
    <DesignCanvas
      title="yaaof · M01 wireframes"
      subtitle="Crisp box-and-line layout exploration. Variants per surface; pick a direction before hi-fi."
    >
      <DCSection id="sidebar" title="Sidebar · user-controlled pin / float"
        subtitle="Single primitive, two user-controlled states. Toggle is the pin button (📌) in the sidebar — common pattern in Linear, VSCode, Slack, etc.">
        <DCArtboard id="sbPin"     label="Pinned (default)"           width={ARTBOARD_W} height={ARTBOARD_H}><PopPinned /></DCArtboard>
        <DCArtboard id="sbFltShut" label="Floating · idle (rail only)" width={ARTBOARD_W} height={ARTBOARD_H}><PopFloatClosed /></DCArtboard>
        <DCArtboard id="sbFltOpen" label="Floating · panel open"      width={ARTBOARD_W} height={ARTBOARD_H}><PopFloat /></DCArtboard>
      </DCSection>

      <DCSection id="dash-onboard" title="Dashboard · onboarding"
        subtitle="First-run, nothing configured yet. 2 layout takes.">
        <DCArtboard id="onA" label="A · Checklist" width={ARTBOARD_W} height={ARTBOARD_H}><DashOnboardA /></DCArtboard>
        <DCArtboard id="onB" label="B · Stepper + preview" width={ARTBOARD_W} height={ARTBOARD_H}><DashOnboardB /></DCArtboard>
      </DCSection>

      <DCSection id="dash-pop" title="Dashboard · populated"
        subtitle="Configured, live data flowing. 3 layout takes.">
        <DCArtboard id="popA" label="A · Metrics-first (Datadog-ish)" width={ARTBOARD_W} height={ARTBOARD_H}><DashPopA /></DCArtboard>
        <DCArtboard id="popB" label="B · Activity-first stream" width={ARTBOARD_W} height={ARTBOARD_H}><DashPopB /></DCArtboard>
        <DCArtboard id="popC" label="C · Tickets-first hero" width={ARTBOARD_W} height={ARTBOARD_H}><DashPopC /></DCArtboard>
      </DCSection>

      <DCSection id="tix" title="Tickets · list"
        subtitle="One layout direction (dense table). The Group-by-Status toggle re-renders the same data with section headers — that's the seam new statuses plug into later.">
        <DCArtboard id="tixA" label="Default · flat" width={ARTBOARD_W} height={ARTBOARD_H}><TicketsA /></DCArtboard>
        <DCArtboard id="tixGrp" label="Toggled · grouped by Status" width={ARTBOARD_W} height={ARTBOARD_H}><TicketsAGrouped /></DCArtboard>
      </DCSection>

      <DCSection id="ticket-agents" title="Ticket detail · Review tab"
        subtitle="Renamed from 'Agents' — describes the status, not the implementation. Header now generalises (kind chip + source line). 3 organization takes for the body.">
        <DCArtboard id="agA" label="A · 3 columns" width={ARTBOARD_W} height={ARTBOARD_H}><AgentsA /></DCArtboard>
        <DCArtboard id="agB" label="B · Vertical, findings inline" width={ARTBOARD_W} height={ARTBOARD_H}><AgentsB /></DCArtboard>
        <DCArtboard id="agC" label="C · Master-detail split" width={ARTBOARD_W} height={ARTBOARD_H}><AgentsC /></DCArtboard>
      </DCSection>

      <DCSection id="ticket-audit" title="Ticket detail · Audit log"
        subtitle="Flight-recorder. 3 representational takes.">
        <DCArtboard id="auA" label="A · Rail + rows (recommended)" width={ARTBOARD_W} height={ARTBOARD_H}><AuditA /></DCArtboard>
        <DCArtboard id="auB" label="B · Table with inline JSON" width={ARTBOARD_W} height={ARTBOARD_H}><AuditB /></DCArtboard>
        <DCArtboard id="auC" label="C · Per-agent swimlanes" width={ARTBOARD_W} height={ARTBOARD_H}><AuditC /></DCArtboard>
      </DCSection>

      <DCSection id="rest" title="Other surfaces · single take each"
        subtitle="Memory, Prompts, Repos, Settings. One layout each — happy to push more if needed.">
        <DCArtboard id="mem"  label="Memory"   width={ARTBOARD_W} height={ARTBOARD_H}><Memory /></DCArtboard>
        <DCArtboard id="pmt"  label="Prompts"  width={ARTBOARD_W} height={ARTBOARD_H}><Prompts /></DCArtboard>
        <DCArtboard id="rep"  label="Repos"    width={ARTBOARD_W} height={ARTBOARD_H}><Repos /></DCArtboard>
        <DCArtboard id="set"  label="Settings" width={ARTBOARD_W} height={ARTBOARD_H}><Settings /></DCArtboard>
      </DCSection>
    </DesignCanvas>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<WireApp />);
