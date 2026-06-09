package supervisor

import (
	"os"
	"path/filepath"
	"sort"
	"testing"
)

// plantWorkspace mimics what `workspace.RealHandler.ProvisionWorkspace` does
// on disk: an `os.MkdirTemp`-style tempdir with a `.workspace-id`
// manifest file at the top.
func plantWorkspace(t *testing.T, root, workspaceID string) string {
	t.Helper()
	dir, err := os.MkdirTemp(root, "yaaos-ws-")
	if err != nil {
		t.Fatalf("plant: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, WorkspaceManifestName), []byte(workspaceID), 0o600); err != nil {
		t.Fatalf("plant manifest: %v", err)
	}
	return dir
}

func TestScanOrphanWorkspaces_FindsPlantedDirs(t *testing.T) {
	root := t.TempDir()
	plantWorkspace(t, root, "ws-a")
	plantWorkspace(t, root, "ws-b")
	plantWorkspace(t, root, "ws-c")

	got, paths := scanOrphanWorkspaces(root, nil)
	ids := make([]string, len(got))
	for i, e := range got {
		ids[i] = e.WorkspaceID
		if e.Status != "unknown" {
			t.Errorf("status: want unknown, got %q", e.Status)
		}
	}
	sort.Strings(ids)
	want := []string{"ws-a", "ws-b", "ws-c"}
	if len(ids) != 3 {
		t.Fatalf("want 3 entries, got %d: %v", len(ids), ids)
	}
	for i := range want {
		if ids[i] != want[i] {
			t.Errorf("orphan %d: want %s got %s", i, want[i], ids[i])
		}
		// path map must carry an entry for each orphan + must point at
		// a real dir.
		p, ok := paths[want[i]]
		if !ok {
			t.Errorf("path map missing entry for %s", want[i])
			continue
		}
		if _, err := os.Stat(p); err != nil {
			t.Errorf("path map points at non-existent dir %s: %v", p, err)
		}
	}
}

func TestScanOrphanWorkspaces_EmptyRootReturnsNil(t *testing.T) {
	root := t.TempDir()
	got, paths := scanOrphanWorkspaces(root, nil)
	if len(got) != 0 {
		t.Errorf("want no orphans in empty root, got %v", got)
	}
	if len(paths) != 0 {
		t.Errorf("want empty path map, got %v", paths)
	}
}

func TestScanOrphanWorkspaces_MissingRootIsOK(t *testing.T) {
	got, paths := scanOrphanWorkspaces("/does/not/exist/yaaos", nil)
	if len(got) != 0 || len(paths) != 0 {
		t.Errorf("missing root should return nil; got %v / %v", got, paths)
	}
}

func TestScanOrphanWorkspaces_EmptyRootStringSkips(t *testing.T) {
	got, paths := scanOrphanWorkspaces("", nil)
	if got != nil || paths != nil {
		t.Errorf("empty root should skip; got %v / %v", got, paths)
	}
}

func TestScanOrphanWorkspaces_SkipsDirsWithoutManifest(t *testing.T) {
	root := t.TempDir()
	// Real workspace.
	plantWorkspace(t, root, "ws-real")
	// Unrelated dir — no manifest. Should be skipped silently.
	unrelated := filepath.Join(root, "not-a-workspace")
	_ = os.Mkdir(unrelated, 0o755)
	// File at root level (not a dir) — also skipped.
	_ = os.WriteFile(filepath.Join(root, "stray.log"), []byte("x"), 0o600)

	got, paths := scanOrphanWorkspaces(root, nil)
	if len(got) != 1 || got[0].WorkspaceID != "ws-real" {
		t.Errorf("want only ws-real, got %v", got)
	}
	if _, ok := paths["ws-real"]; !ok {
		t.Errorf("path map should track ws-real, got %v", paths)
	}
}

func TestScanOrphanWorkspaces_SkipsEmptyManifest(t *testing.T) {
	root := t.TempDir()
	dir, _ := os.MkdirTemp(root, "yaaos-ws-")
	_ = os.WriteFile(filepath.Join(dir, WorkspaceManifestName), []byte("   \n"), 0o600)

	got, _ := scanOrphanWorkspaces(root, nil)
	if len(got) != 0 {
		t.Errorf("empty manifest should be skipped, got %v", got)
	}
}

func TestScanOrphanWorkspaces_TrimsWhitespace(t *testing.T) {
	root := t.TempDir()
	dir, _ := os.MkdirTemp(root, "yaaos-ws-")
	_ = os.WriteFile(filepath.Join(dir, WorkspaceManifestName), []byte("\n  ws-trim  \n"), 0o600)

	got, paths := scanOrphanWorkspaces(root, nil)
	if len(got) != 1 || got[0].WorkspaceID != "ws-trim" {
		t.Errorf("want ws-trim, got %v", got)
	}
	if _, ok := paths["ws-trim"]; !ok {
		t.Errorf("path map should track ws-trim, got %v", paths)
	}
}

// ── Disk janitor ────────────────────────────────────────────────────────

func TestCleanupForgottenWorkspaces_RemovesNamedPaths(t *testing.T) {
	root := t.TempDir()
	dirA := plantWorkspace(t, root, "ws-a")
	dirB := plantWorkspace(t, root, "ws-b")
	dirC := plantWorkspace(t, root, "ws-c")
	paths := map[string]string{"ws-a": dirA, "ws-b": dirB, "ws-c": dirC}

	out := cleanupForgottenWorkspaces(paths, []string{"ws-a", "ws-c"}, nil)

	if _, err := os.Stat(dirA); !os.IsNotExist(err) {
		t.Errorf("dirA should be removed, got %v", err)
	}
	if _, err := os.Stat(dirC); !os.IsNotExist(err) {
		t.Errorf("dirC should be removed, got %v", err)
	}
	if _, err := os.Stat(dirB); err != nil {
		t.Errorf("dirB should survive, got %v", err)
	}
	// The returned map should retain only the not-forgotten entries.
	if len(out) != 1 || out["ws-b"] != dirB {
		t.Errorf("surviving map: want only ws-b, got %v", out)
	}
}

func TestCleanupForgottenWorkspaces_UnknownIdSkipped(t *testing.T) {
	root := t.TempDir()
	dirA := plantWorkspace(t, root, "ws-a")
	paths := map[string]string{"ws-a": dirA}

	// "ws-ghost" isn't in paths — silent skip, no error.
	out := cleanupForgottenWorkspaces(paths, []string{"ws-ghost"}, nil)
	if _, ok := out["ws-a"]; !ok {
		t.Errorf("ws-a should survive, got %v", out)
	}
	if _, err := os.Stat(dirA); err != nil {
		t.Errorf("dirA should not be touched, got %v", err)
	}
}

func TestCleanupForgottenWorkspaces_EmptyForgottenIsNoop(t *testing.T) {
	paths := map[string]string{"ws-a": "/tmp/yaaos-fake"}
	out := cleanupForgottenWorkspaces(paths, nil, nil)
	if len(out) != 1 || out["ws-a"] != "/tmp/yaaos-fake" {
		t.Errorf("empty forgotten should leave map untouched, got %v", out)
	}
}

func TestCleanupForgottenWorkspaces_ReturnsNewMap_DoesntMutateInput(t *testing.T) {
	root := t.TempDir()
	dirA := plantWorkspace(t, root, "ws-a")
	paths := map[string]string{"ws-a": dirA}

	out := cleanupForgottenWorkspaces(paths, []string{"ws-a"}, nil)

	// Removed from the returned map.
	if _, ok := out["ws-a"]; ok {
		t.Errorf("ws-a should be removed from returned map, got %v", out)
	}
	// Input map is NOT mutated — caller swaps maps explicitly.
	if _, ok := paths["ws-a"]; !ok {
		t.Errorf("input map should not be mutated; got %v", paths)
	}
}
