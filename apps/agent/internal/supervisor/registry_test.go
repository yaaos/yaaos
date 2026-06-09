// Registry unit tests: workspace state machine, read methods, Dispatch registry effects.
package supervisor

import (
	"context"
	"testing"

	"github.com/yaaos/agent/internal/protocol"
	"github.com/yaaos/agent/internal/workspace/workspacetest"
)

// ── WorkspaceState machine ──────────────────────────────────────────────────

func TestRegistry_CreateActive_SnapshotRunning(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)

	p.createActive("ws-1", nil)
	snap := p.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("want 1 entry, got %d", len(snap))
	}
	if snap[0].WorkspaceID != "ws-1" {
		t.Errorf("workspace_id: want ws-1 got %q", snap[0].WorkspaceID)
	}
	if snap[0].Status != "running" {
		t.Errorf("status: want running got %q", snap[0].Status)
	}
	if snap[0].CurrentCommandID != "" {
		t.Errorf("current_command_id: want empty got %q", snap[0].CurrentCommandID)
	}
}

func TestRegistry_SeedOrphan_SnapshotUnknown(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)

	p.seedOrphan("ws-orphan", "/tmp/ws-orphan")
	snap := p.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("want 1 entry, got %d", len(snap))
	}
	if snap[0].Status != "unknown" {
		t.Errorf("status: want unknown got %q", snap[0].Status)
	}
}

func TestRegistry_MarkDefunct_FlipsActiveToDefunct(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)

	p.createActive("ws-1", nil)
	p.markDefunct("ws-1")

	snap := p.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("want 1 entry, got %d", len(snap))
	}
	if snap[0].Status != "exited" {
		t.Errorf("status: want exited got %q", snap[0].Status)
	}
}

func TestRegistry_MarkDefunct_MidIdle_StaysInKnownIDs(t *testing.T) {
	// A Defunct workspace keeps its id in KnownIDs — the disk sweep
	// must not remove its directory while it's still in the registry.
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)

	p.createActive("ws-1", nil)
	p.markDefunct("ws-1")

	known := p.KnownIDs()
	if _, ok := known["ws-1"]; !ok {
		t.Errorf("Defunct workspace should still be in KnownIDs, got %v", known)
	}
}

func TestRegistry_SetPathRoundtrip(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	p.createActive("ws-1", nil)
	p.setPath("ws-1", "/workspace/ws-1")

	paths := p.Paths()
	if paths["ws-1"] != "/workspace/ws-1" {
		t.Errorf("Paths: want /workspace/ws-1 got %q", paths["ws-1"])
	}
}

func TestRegistry_Remove_DropsRecord(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	p.createActive("ws-1", nil)
	p.remove("ws-1")

	snap := p.Snapshot()
	if len(snap) != 0 {
		t.Errorf("want empty snapshot after remove, got %d entries", len(snap))
	}
	known := p.KnownIDs()
	if _, ok := known["ws-1"]; ok {
		t.Errorf("removed workspace should not be in KnownIDs")
	}
}

func TestRegistry_SetCommandID_ReflectsInSnapshot(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	p.createActive("ws-1", nil)
	p.setCommandID("ws-1", "cmd-42")

	snap := p.Snapshot()
	if snap[0].CurrentCommandID != "cmd-42" {
		t.Errorf("current_command_id: want cmd-42 got %q", snap[0].CurrentCommandID)
	}
	// Status still running — busy-ness is orthogonal to liveness.
	if snap[0].Status != "running" {
		t.Errorf("status: want running got %q", snap[0].Status)
	}
}

func TestRegistry_ClearCommandID_EmptiesField(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	p.createActive("ws-1", nil)
	p.setCommandID("ws-1", "cmd-42")
	p.clearCommandID("ws-1")

	snap := p.Snapshot()
	if snap[0].CurrentCommandID != "" {
		t.Errorf("current_command_id: want empty got %q", snap[0].CurrentCommandID)
	}
}

// ── KnownIDs covers all states ──────────────────────────────────────────────

func TestRegistry_KnownIDs_AllStates(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	p.createActive("ws-active", nil)
	p.seedOrphan("ws-orphan", "/tmp/orphan")
	p.createActive("ws-defunct", nil)
	p.markDefunct("ws-defunct")

	known := p.KnownIDs()
	for _, id := range []string{"ws-active", "ws-orphan", "ws-defunct"} {
		if _, ok := known[id]; !ok {
			t.Errorf("KnownIDs missing %q; got %v", id, known)
		}
	}
}

// ── Dispatch registry effects ───────────────────────────────────────────────

// TestDispatch_Create_RegistryActiveAndPathSet proves that a successful
// ProvisionWorkspace dispatch installs an Active record and sets the path from
// ProvisionResult.
func TestDispatch_Create_RegistryActiveAndPathSet(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	defer p.CloseAll(context.Background())

	ev := p.Dispatch(context.Background(), newCreateCmd("ws-1", "cmd-1"), nil, 0)
	if ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("dispatch: want completed_success got %q (reason=%q)", ev.Kind, ev.FailureReason)
	}

	snap := p.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("want 1 registry entry, got %d", len(snap))
	}
	if snap[0].Status != "running" {
		t.Errorf("status: want running got %q", snap[0].Status)
	}
	if snap[0].CurrentCommandID != "" {
		t.Errorf("current_command_id should be cleared after dispatch, got %q", snap[0].CurrentCommandID)
	}
	// StubHandler.ProvisionWorkspace returns path="/stub/ws-1" — verify it's set.
	paths := p.Paths()
	if paths["ws-1"] == "" {
		t.Errorf("path should be set after create; paths=%v", paths)
	}
}

// TestDispatch_NonCreate_UnknownWorkspace_ErrUnknown verifies that a
// non-create command for a workspace with no registry record yields
// completed_failure.
func TestDispatch_NonCreate_UnknownWorkspace_ErrUnknown(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	defer p.CloseAll(context.Background())

	ev := p.Dispatch(context.Background(), newWriteCmd("ws-never", "cmd-1"), nil, 0)
	if ev.Kind != protocol.EventCompletedFailure {
		t.Fatalf("want completed_failure for unknown workspace, got %q", ev.Kind)
	}
}

// TestDispatch_Cleanup_RemovesRecord verifies that a successful CleanupWorkspace
// dispatch removes the registry record.
func TestDispatch_Cleanup_RemovesRecord(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	defer p.CloseAll(context.Background())

	p.Dispatch(context.Background(), newCreateCmd("ws-1", "cmd-create"), nil, 0)
	p.Dispatch(context.Background(), newCleanupCmd("ws-1", "cmd-cleanup"), nil, 0)

	snap := p.Snapshot()
	if len(snap) != 0 {
		t.Errorf("want empty snapshot after cleanup, got %d entries", len(snap))
	}
	known := p.KnownIDs()
	if _, ok := known["ws-1"]; ok {
		t.Errorf("cleaned-up workspace should not be in KnownIDs")
	}
}

// TestRegistry_ActiveIDs_OnlyActive verifies that ActiveIDs returns only
// Active-state workspace IDs.
func TestRegistry_ActiveIDs_OnlyActive(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	p.createActive("ws-active", nil)
	p.createActive("ws-defunct", nil)
	p.markDefunct("ws-defunct")
	p.seedOrphan("ws-orphan", "/tmp/orphan")

	ids := p.ActiveIDs()
	if len(ids) != 1 || ids[0] != "ws-active" {
		t.Errorf("ActiveIDs: want [ws-active], got %v", ids)
	}
}

// TestRegistry_Paths_AllStates verifies that Paths returns paths for records
// that have a path set (both Active and Orphaned).
func TestRegistry_Paths_AllStates(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	p.createActive("ws-active", nil)
	p.setPath("ws-active", "/ws/active")
	p.seedOrphan("ws-orphan", "/ws/orphan")
	// Defunct without path.
	p.createActive("ws-defunct", nil)
	p.markDefunct("ws-defunct")

	paths := p.Paths()
	if paths["ws-active"] != "/ws/active" {
		t.Errorf("ws-active path: want /ws/active got %q", paths["ws-active"])
	}
	if paths["ws-orphan"] != "/ws/orphan" {
		t.Errorf("ws-orphan path: want /ws/orphan got %q", paths["ws-orphan"])
	}
}

// ── Supervisor-level success-signal test ───────────────────────────────────

// TestSupervisor_IdleWorkspace_KnownAndHeartbeatedRunning is the
// success-signal test: a workspace created via Dispatch is in KnownIDs,
// is not removed by a disk sweep, and Snapshot reports status="running"
// with empty current_command_id while idle.
func TestSupervisor_IdleWorkspace_KnownAndHeartbeatedRunning(t *testing.T) {
	root := t.TempDir()
	plantWorkspace(t, root, "ws-a")

	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	defer p.CloseAll(context.Background())

	ev := p.Dispatch(context.Background(), newCreateCmd("ws-a", "cmd-1"), nil, 0)
	if ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("create: %q (reason=%q)", ev.Kind, ev.FailureReason)
	}

	// Idle workspace: Snapshot reports running + empty current_command_id.
	snap := p.Snapshot()
	var entry *protocol.HeartbeatWorkspaceEntry
	for i := range snap {
		if snap[i].WorkspaceID == "ws-a" {
			entry = &snap[i]
			break
		}
	}
	if entry == nil {
		t.Fatalf("ws-a not in Snapshot: %v", snap)
	}
	if entry.Status != "running" {
		t.Errorf("idle workspace: want status=running, got %q", entry.Status)
	}
	if entry.CurrentCommandID != "" {
		t.Errorf("idle workspace: want empty current_command_id, got %q", entry.CurrentCommandID)
	}

	// KnownIDs includes it — sweep won't touch its directory.
	known := p.KnownIDs()
	if _, ok := known["ws-a"]; !ok {
		t.Fatalf("ws-a not in KnownIDs: %v", known)
	}
	removed := sweepOrphanWorkspaceDirs(root, known, nil)
	if removed != 0 {
		t.Errorf("sweep removed %d dirs; want 0 (live workspace should be protected)", removed)
	}
}

// TestRegistry_SeedOrphan_KnownIDs verifies orphan records are in KnownIDs
// so the disk sweep leaves orphan directories alone (the backend decides
// whether to forget them).
func TestRegistry_SeedOrphan_KnownIDs(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	p.seedOrphan("ws-orphan", "/tmp/orphan")

	known := p.KnownIDs()
	if _, ok := known["ws-orphan"]; !ok {
		t.Errorf("orphan should be in KnownIDs, got %v", known)
	}
}

// TestDispatch_MarkDefunct_ChildExitWatcher verifies that after markDefunct
// the id stays in KnownIDs and Snapshot reports status="exited".
func TestDispatch_MarkDefunct_ChildExitWatcher(t *testing.T) {
	p := NewPool(inProcessSpawn(workspacetest.StubHandler{}), nil)
	defer p.CloseAll(context.Background())

	if ev := p.Dispatch(context.Background(), newCreateCmd("ws-exit", "cmd-create"), nil, 0); ev.Kind != protocol.EventCompletedSuccess {
		t.Fatalf("create: %q", ev.Kind)
	}

	// Simulate child-exit watcher firing markDefunct.
	p.markDefunct("ws-exit")

	known := p.KnownIDs()
	if _, ok := known["ws-exit"]; !ok {
		t.Errorf("Defunct workspace should remain in KnownIDs; got %v", known)
	}
	snap := p.Snapshot()
	found := false
	for _, e := range snap {
		if e.WorkspaceID == "ws-exit" {
			found = true
			if e.Status != "exited" {
				t.Errorf("status: want exited got %q", e.Status)
			}
		}
	}
	if !found {
		t.Errorf("ws-exit not in Snapshot after markDefunct: %v", snap)
	}
}
