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
	Path    string
	Repo    string
	HeadSHA string
	Branch  string
	Reused  bool
}

// ToWire returns the map[string]any the backend expects from ProvisionWorkspace.
// Keys match the legacy realhandler.go output so the backend contract is unchanged.
func (r ProvisionResult) ToWire() map[string]any {
	return map[string]any{
		"path":     r.Path,
		"repo":     r.Repo,
		"head_sha": r.HeadSHA,
		"branch":   r.Branch,
		"reused":   r.Reused,
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
type InvokeResult struct {
	WorkspaceID string
	ExecResult
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

// SkillManifestEntry is one element of the enumerated skills list returned by
// the EnumerateSkills command. The backend persists this into
// claude_code_repos.skills as JSONB.
type SkillManifestEntry struct {
	// Name is the invocation handle: the directory name for repo-local skills,
	// or "<plugin>:<skill>" for plugin-sourced skills.
	Name       string  `json:"name"`
	Source     string  `json:"source"`      // "repo" | "plugin"
	PluginName *string `json:"plugin_name"` // nil for source=="repo"
}

// EnumerateSkillsResult is the typed output of the EnumerateSkills command.
type EnumerateSkillsResult struct {
	WorkspaceID string
	Skills      []SkillManifestEntry
}

// ToWire returns the map[string]any the backend expects from EnumerateSkills.
// The backend's PersistSkillManifest step reads `outputs["skills"]` as a list
// of plain maps. `plugin_name` is a string or nil (never a pointer) so the
// backend can JSON-round-trip it cleanly.
func (r EnumerateSkillsResult) ToWire() map[string]any {
	skills := make([]map[string]any, 0, len(r.Skills))
	for _, s := range r.Skills {
		var pluginName any
		if s.PluginName != nil {
			pluginName = *s.PluginName
		}
		entry := map[string]any{
			"name":        s.Name,
			"source":      s.Source,
			"plugin_name": pluginName,
		}
		skills = append(skills, entry)
	}
	return map[string]any{
		"workspace_id": r.WorkspaceID,
		"skills":       skills,
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
