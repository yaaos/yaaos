package command

import "time"

// Result is the typed return from every command's Execute. ToWire() produces
// the map[string]any placed verbatim in AgentEvent.Outputs — the backend wire
// contract stays map[string]any while the Go command↔supervisor boundary is
// fully typed.
type Result interface {
	ToWire() map[string]any
}

// ExecResult holds the outcome of a subprocess invocation. Embedded in
// InvokeResult (and in future ProvisionResult when git clone is subprocess-based).
type ExecResult struct {
	ExitCode int
	Stdout   string
	Stderr   string
	Duration time.Duration
}

// ProvisionResult is the typed output of ProvisionWorkspace. Path carries the
// workspace path the supervisor registry keys on; Reused signals an idempotent repeat.
type ProvisionResult struct {
	WorkspaceID string
	Path        string
	Repo        string
	HeadSHA     string
	Branch      string
	Reused      bool
}

// ToWire returns the map[string]any the backend expects from ProvisionWorkspace.
// workspace_id is required so the router can resolve "$provision.workspace_id"
// when dispatching CodeReview and CleanupWorkspace.
func (r ProvisionResult) ToWire() map[string]any {
	return map[string]any{
		"workspace_id": r.WorkspaceID,
		"path":         r.Path,
		"repo":         r.Repo,
		"head_sha":     r.HeadSHA,
		"branch":       r.Branch,
		"reused":       r.Reused,
	}
}

// WriteFilesResult is the typed output of WriteFiles.
type WriteFilesResult struct {
	WorkspaceID string
	FilesCount  int
}

// ToWire returns the map[string]any the backend expects from WriteFiles.
func (r WriteFilesResult) ToWire() map[string]any {
	return map[string]any{
		"workspace_id": r.WorkspaceID,
		"files_count":  r.FilesCount,
	}
}

// RefreshResult is the typed output of RefreshWorkspaceAuth.
type RefreshResult struct {
	WorkspaceID string
	Refreshed   bool
}

// ToWire returns the map[string]any the backend expects from RefreshWorkspaceAuth.
func (r RefreshResult) ToWire() map[string]any {
	return map[string]any{
		"workspace_id": r.WorkspaceID,
		"refreshed":    r.Refreshed,
	}
}

// InvokeResult is the typed output of InvokeClaudeCode. Embeds ExecResult for
// the subprocess outcome plus a workspace identifier.
//
// Artifact/ArtifactError carry the agent-collected `$TMPDIR/<command_id>.md`
// content (or the reason it's absent) to the AgentEvent's top-level
// `artifact`/`artifact_error` fields — NOT part of ToWire()'s Outputs map,
// since those are distinct wire fields alongside `outputs`, not inside it.
// Populated even when RunClaude ultimately returns an error (e.g. a push
// failure after a successful invocation) — see ArtifactPayload.
type InvokeResult struct {
	WorkspaceID string
	ExecResult
	Artifact      *string
	ArtifactError string
}

// ArtifactPayload implements ArtifactResult. workspace.executeCommand calls
// this (on both the success and error return paths) to populate the
// AgentEvent's top-level artifact fields — distinct from the ToWire() map,
// which only survives on the success path.
func (r InvokeResult) ArtifactPayload() (body *string, artifactError string) {
	return r.Artifact, r.ArtifactError
}

// ToWire returns the map[string]any the backend expects from InvokeClaudeCode.
// The backend's CodeReview step parses the full stdout, so it is included
// untruncated; stdout_excerpt is a display-friendly subset for operators.
func (r InvokeResult) ToWire() map[string]any {
	stdoutExcerpt := r.Stdout
	if len(stdoutExcerpt) > 16*1024 {
		stdoutExcerpt = stdoutExcerpt[:16*1024] + "...[truncated]"
	}
	return map[string]any{
		"workspace_id":   r.WorkspaceID,
		"exit_code":      r.ExitCode,
		"duration_ms":    r.Duration.Milliseconds(),
		"stdout":         r.Stdout,
		"stderr":         r.Stderr,
		"stdout_excerpt": stdoutExcerpt,
	}
}

// ArtifactResult is implemented by command Results that can carry a
// collected artifact body (today: InvokeResult only). workspace.executeCommand
// type-asserts against this interface to populate the AgentEvent's top-level
// artifact fields, which live alongside — not inside — the ToWire() Outputs map.
type ArtifactResult interface {
	ArtifactPayload() (body *string, artifactError string)
}

// CleanupResult is the typed output of CleanupWorkspace.
type CleanupResult struct {
	WorkspaceID string
	Destroyed   bool
	Path        string
	Reason      string
}

// ToWire returns the map[string]any the backend expects from CleanupWorkspace.
func (r CleanupResult) ToWire() map[string]any {
	m := map[string]any{
		"workspace_id": r.WorkspaceID,
		"destroyed":    r.Destroyed,
		"path":         r.Path,
	}
	if r.Reason != "" {
		m["reason"] = r.Reason
	}
	return m
}

// PushBranchResult is the typed output of PushBranch.
type PushBranchResult struct {
	WorkspaceID string
	Pushed      bool
}

// ToWire returns the map[string]any the backend expects from PushBranch.
func (r PushBranchResult) ToWire() map[string]any {
	return map[string]any{
		"workspace_id": r.WorkspaceID,
		"pushed":       r.Pushed,
	}
}

// ConfigUpdateResult is the typed output of ConfigUpdateCommand.Execute.
// The backend doesn't read specific fields from a config-update's outputs;
// an empty map is a valid success response, but we include the max_workspaces
// that was applied for operator visibility.
type ConfigUpdateResult struct {
	MaxWorkspaces int
}

// ToWire returns the map[string]any placed in AgentEvent.Outputs.
func (r ConfigUpdateResult) ToWire() map[string]any {
	return map[string]any{
		"max_workspaces": r.MaxWorkspaces,
	}
}

// ShutdownResult is the typed output of ShutdownCommand.Execute.
// The backend's terminal event for a ShutdownCommand carries no meaningful
// domain outputs; an empty map is the correct success response.
type ShutdownResult struct{}

// ToWire returns the empty outputs map for a ShutdownCommand terminal event.
func (r ShutdownResult) ToWire() map[string]any {
	return map[string]any{}
}

// CancelShutdownResult is the typed output of CancelShutdownCommand.Execute.
type CancelShutdownResult struct{}

// ToWire returns the empty outputs map for a CancelShutdownCommand terminal event.
func (r CancelShutdownResult) ToWire() map[string]any {
	return map[string]any{}
}
