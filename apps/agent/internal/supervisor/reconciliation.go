// Startup reconciliation + disk janitor.
//
// On supervisor restart, workspace directories left over from a previous
// run (process crash, pod replace, OOM-kill) get reattributed via the
// `.workspace-id` manifest file `RealHandler.CreateWorkspace` writes into
// each tempdir at create time. The startup scan reports each as
// `status="unknown"` in the first heartbeat (slice 71); the backend
// responds with a `forgotten_workspaces` list naming the ones it no
// longer tracks. This file:
//
//   - `scanOrphanWorkspaces(root)` — startup scan, returns the
//     heartbeat entries + a workspace_id → path map (so the janitor
//     can find each dir later).
//   - `cleanupForgottenWorkspaces(paths, forgotten, log)` — disk
//     janitor (slice 75). `os.RemoveAll` for each path the backend
//     says is forgotten; returns the surviving paths so the caller
//     can drop them from its internal map.
//
// No directory-name parsing anywhere — manifest files survive across
// `os.MkdirTemp` implementation changes and are language-agnostic.

package supervisor

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/yaaos/agent/internal/protocol"
)

// WorkspaceManifestName is the filename the workspace handler writes
// inside each tempdir containing the workspace_id. Read on startup so
// orphan dirs can be reattributed without parsing dir names.
const WorkspaceManifestName = ".workspace-id"

// scanOrphanWorkspaces walks `root` one level deep, looks for
// `<dir>/.workspace-id` manifest files, and returns:
//   - a heartbeat-entry list for each (status="unknown")
//   - a workspace_id → absolute-path map so the disk janitor can later
//     `os.RemoveAll` the right dir when the backend signals forgotten
//
// Missing root / unreadable directory entries are logged + skipped —
// startup reconciliation is best-effort by design.
func scanOrphanWorkspaces(root string, log Logger) ([]protocol.HeartbeatWorkspaceEntry, map[string]string) {
	if root == "" {
		return nil, nil
	}
	if log == nil {
		log = nullLogger{}
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		// Missing root is normal on a fresh pod; log at info, not warn.
		if os.IsNotExist(err) {
			log.Info("reconcile.scan_skipped", "reason", "root_missing", "root", root)
			return nil, nil
		}
		log.Warn("reconcile.scan_failed", "root", root, "err", err.Error())
		return nil, nil
	}
	var out []protocol.HeartbeatWorkspaceEntry
	paths := map[string]string{}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		dir := filepath.Join(root, e.Name())
		manifestPath := filepath.Join(dir, WorkspaceManifestName)
		raw, err := os.ReadFile(manifestPath)
		if err != nil {
			// Not every dir under root has to be a workspace; missing
			// manifest = silent skip. Other read errors → warn.
			if !os.IsNotExist(err) {
				log.Warn("reconcile.manifest_read_failed",
					"path", manifestPath, "err", err.Error())
			}
			continue
		}
		id := strings.TrimSpace(string(raw))
		if id == "" {
			log.Warn("reconcile.empty_manifest", "path", manifestPath)
			continue
		}
		out = append(out, protocol.HeartbeatWorkspaceEntry{
			WorkspaceID: id,
			Status:      "unknown",
		})
		paths[id] = dir
		log.Info("reconcile.orphan_found", "workspace_id", id, "path", dir)
	}
	return out, paths
}

// cleanupForgottenWorkspaces removes the on-disk directories the backend
// said it no longer tracks. `paths` is the workspace_id → path map the
// supervisor built at scan time + augments with new CreateWorkspace
// rows (so live workspaces are eligible for cleanup too — the backend
// names them in `forgotten_workspaces` when their workflow already
// terminated). Removes that succeed are removed from the returned map;
// the caller swaps its own map for the returned one.
//
// Best-effort: a remove failure logs at warn and leaves the entry in
// the map so the next heartbeat retries. An unknown workspace_id in
// `forgotten` (one we don't have a path for) is logged at info and
// skipped — likely the backend forgot something the agent already
// cleaned up.
func cleanupForgottenWorkspaces(paths map[string]string, forgotten []string, log Logger) map[string]string {
	if log == nil {
		log = nullLogger{}
	}
	out := make(map[string]string, len(paths))
	for k, v := range paths {
		out[k] = v
	}
	for _, id := range forgotten {
		path, ok := out[id]
		if !ok {
			log.Info("janitor.unknown_forgotten_id", "workspace_id", id)
			continue
		}
		if err := os.RemoveAll(path); err != nil {
			log.Warn("janitor.remove_failed", "workspace_id", id, "path", path, "err", err.Error())
			continue
		}
		delete(out, id)
		log.Info("janitor.removed_orphan", "workspace_id", id, "path", path)
	}
	return out
}
