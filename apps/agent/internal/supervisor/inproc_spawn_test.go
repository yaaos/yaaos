package supervisor

import (
	"context"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/supervisor/supervisortest"
)

// inProcessSpawn wraps supervisortest.InProcessSpawn, adapting its return type
// to the supervisor.SpawnFunc / supervisor.WorkspaceRunner types. supervisortest
// cannot import supervisor (import cycle), so this thin adapter lives here.
func inProcessSpawn(ops command.WorkspaceOps) SpawnFunc {
	raw := supervisortest.InProcessSpawn(ops)
	return func(ctx context.Context, workspaceID string) (WorkspaceRunner, error) {
		return raw(ctx, workspaceID)
	}
}
