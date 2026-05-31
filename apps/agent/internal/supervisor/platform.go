package supervisor

import "runtime"

// goOS returns the GOOS value as the OS metadata field.
// Simple: the agent always runs as a compiled binary so GOOS is accurate.
func goOS() string { return runtime.GOOS }
