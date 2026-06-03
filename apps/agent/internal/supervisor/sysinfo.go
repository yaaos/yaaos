package supervisor

import (
	"bytes"
	"os"
	"runtime"
	"strconv"
)

// Canonical paths probed by memoryBytes. Overridable in tests via memoryBytesFrom.
const (
	cgroupV2MemoryMaxPath = "/sys/fs/cgroup/memory.max"
	procMeminfoPath       = "/proc/meminfo"
)

// cpuCount returns the number of logical CPUs available to this process.
// runtime.NumCPU honors GOMAXPROCS and (Go 1.25+) the cgroup CPU quota, so
// in a containerized agent this is the task's CPU allocation, not the host's.
func cpuCount() int { return runtime.NumCPU() }

// memoryBytes returns the memory ceiling for this agent in bytes.
// Containerized: the cgroup v2 memory limit. Bare host: /proc/meminfo MemTotal.
// 0 when neither is readable — sent as NULL on the wire, hidden in the UI.
func memoryBytes() int64 {
	return memoryBytesFrom(cgroupV2MemoryMaxPath, procMeminfoPath)
}

// memoryBytesFrom is the testable form of memoryBytes. It reads the cgroup v2
// limit first (containerized case) and falls back to /proc/meminfo MemTotal
// (bare host or "max" cgroup value). Anything unparseable returns 0 — the
// agent never invents a number.
func memoryBytesFrom(cgroupPath, meminfoPath string) int64 {
	if n := readCgroupV2MemoryMax(cgroupPath); n > 0 {
		return n
	}
	data, err := os.ReadFile(meminfoPath)
	if err != nil {
		return 0
	}
	return parseMeminfoMemTotal(data)
}

// readCgroupV2MemoryMax parses /sys/fs/cgroup/memory.max. Returns 0 for any
// non-finite value: the literal "max" (no limit set), file missing, or parse
// failure — caller falls back to /proc/meminfo.
func readCgroupV2MemoryMax(path string) int64 {
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("max")) {
		return 0
	}
	n, err := strconv.ParseInt(string(trimmed), 10, 64)
	if err != nil || n <= 0 {
		return 0
	}
	return n
}

// parseMeminfoMemTotal extracts the MemTotal value from /proc/meminfo contents
// and converts the kB count to bytes. Returns 0 when MemTotal is absent or
// the line is malformed.
func parseMeminfoMemTotal(data []byte) int64 {
	const prefix = "MemTotal:"
	for _, line := range bytes.Split(data, []byte("\n")) {
		if !bytes.HasPrefix(line, []byte(prefix)) {
			continue
		}
		// Line shape: "MemTotal:       16384000 kB"
		fields := bytes.Fields(line[len(prefix):])
		if len(fields) < 1 {
			return 0
		}
		kb, err := strconv.ParseInt(string(fields[0]), 10, 64)
		if err != nil || kb <= 0 {
			return 0
		}
		return kb * 1024
	}
	return 0
}
