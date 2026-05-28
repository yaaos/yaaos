package activity

import "sync"

// WorkspaceMapping caches the workspace_id → workflow_execution_id
// translation that the backend ships in `subscribe` messages.
// Outbound `activity_batch` frames need the workflow id
// to address the right SSE channel, but the agent only ever knows
// workspace ids — the mapping closes that gap without a backend
// round-trip per batch.
//
// Lifecycle: Set on subscribe, Remove on unsubscribe, read on every
// outbound batch. Reads outnumber writes by ~3 orders of magnitude
// at 250ms batching, so RWMutex fits.
type WorkspaceMapping struct {
	mu sync.RWMutex
	m  map[string]string
}

func NewWorkspaceMapping() *WorkspaceMapping {
	return &WorkspaceMapping{m: make(map[string]string)}
}

func (w *WorkspaceMapping) Set(workspaceID, workflowExecutionID string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.m[workspaceID] = workflowExecutionID
}

func (w *WorkspaceMapping) Get(workspaceID string) (string, bool) {
	w.mu.RLock()
	defer w.mu.RUnlock()
	wf, ok := w.m[workspaceID]
	return wf, ok
}

func (w *WorkspaceMapping) Remove(workspaceID string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	delete(w.m, workspaceID)
}

func (w *WorkspaceMapping) Size() int {
	w.mu.RLock()
	defer w.mu.RUnlock()
	return len(w.m)
}
