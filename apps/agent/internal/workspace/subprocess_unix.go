//go:build unix

package workspace

import "syscall"

// procAttrNewPGroupWS spawns the subprocess in its own process group so
// SIGTERM/SIGKILL on the group reaches any grand-children. Mirrors
// supervisor.procAttrNewPGroup; duplicated here because Go's internal/
// rules block workspace importing supervisor.
func procAttrNewPGroupWS() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true}
}

// killGroupWS sends sig to the process group identified by pid.
func killGroupWS(pid int, sig syscall.Signal) {
	_ = syscall.Kill(-pid, sig)
}
