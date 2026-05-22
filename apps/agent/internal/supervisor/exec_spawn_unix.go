//go:build unix

package supervisor

import "syscall"

// procAttrNewPGroup spawns the workspace subprocess in its own process
// group. That way `killGroup(pid, sig)` reaches the Claude Code
// subprocess + any grand-children too.
func procAttrNewPGroup() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true}
}

// killGroup sends `sig` to the process group identified by `pid`. The
// minus-sign convention tells the kernel to deliver the signal to the
// whole group.
func killGroup(pid int, sig syscall.Signal) {
	_ = syscall.Kill(-pid, sig)
}
