package supervisor

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// TestCPUCount returns the Go-runtime view of logical CPUs.
// runtime.NumCPU honors GOMAXPROCS and (since Go 1.25) the cgroup CPU quota,
// so this is the right answer for "capacity this agent can actually use."
func TestCPUCount(t *testing.T) {
	got := cpuCount()
	if got <= 0 {
		t.Errorf("cpuCount(): want > 0, got %d", got)
	}
	if got != runtime.NumCPU() {
		t.Errorf("cpuCount(): want %d (runtime.NumCPU), got %d", runtime.NumCPU(), got)
	}
}

// TestMemoryBytesFrom_CgroupV2Limit verifies the cgroup v2 limit wins over
// /proc/meminfo when both exist and the cgroup has a finite value. This is the
// containerized case — the task limit, not the host RAM, is what we report.
func TestMemoryBytesFrom_CgroupV2Limit(t *testing.T) {
	dir := t.TempDir()
	cgroup := filepath.Join(dir, "memory.max")
	meminfo := filepath.Join(dir, "meminfo")
	if err := os.WriteFile(cgroup, []byte("2147483648\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(meminfo, []byte("MemTotal:       65536000 kB\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got := memoryBytesFrom(cgroup, meminfo)
	if got != 2147483648 {
		t.Errorf("memoryBytesFrom: want 2147483648 (cgroup wins), got %d", got)
	}
}

// TestMemoryBytesFrom_CgroupV2Unlimited verifies that the literal "max" in
// memory.max means "no limit" and the function falls back to /proc/meminfo.
// This is the host case — agent runs on a bare VM with no cgroup memory cap.
func TestMemoryBytesFrom_CgroupV2Unlimited(t *testing.T) {
	dir := t.TempDir()
	cgroup := filepath.Join(dir, "memory.max")
	meminfo := filepath.Join(dir, "meminfo")
	if err := os.WriteFile(cgroup, []byte("max\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// 1024 kB = 1048576 bytes
	if err := os.WriteFile(meminfo, []byte("MemTotal:           1024 kB\nMemFree: 512 kB\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got := memoryBytesFrom(cgroup, meminfo)
	if got != 1024*1024 {
		t.Errorf("memoryBytesFrom: want 1048576 (meminfo fallback), got %d", got)
	}
}

// TestMemoryBytesFrom_NoCgroupFile falls back to /proc/meminfo when
// memory.max doesn't exist (host without cgroup v2 mounted at the canonical
// path, or a darwin developer environment).
func TestMemoryBytesFrom_NoCgroupFile(t *testing.T) {
	dir := t.TempDir()
	cgroup := filepath.Join(dir, "does-not-exist")
	meminfo := filepath.Join(dir, "meminfo")
	if err := os.WriteFile(meminfo, []byte("MemTotal:       2048 kB\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got := memoryBytesFrom(cgroup, meminfo)
	if got != 2048*1024 {
		t.Errorf("memoryBytesFrom: want %d (meminfo), got %d", 2048*1024, got)
	}
}

// TestMemoryBytesFrom_NeitherAvailable returns 0 when nothing is readable.
// The metadata field is omitempty on the wire so the backend sees NULL and
// the UI hides the row — no fake number is ever reported.
func TestMemoryBytesFrom_NeitherAvailable(t *testing.T) {
	dir := t.TempDir()
	got := memoryBytesFrom(filepath.Join(dir, "nope1"), filepath.Join(dir, "nope2"))
	if got != 0 {
		t.Errorf("memoryBytesFrom: want 0, got %d", got)
	}
}

// TestMemoryBytesFrom_CgroupParseError treats unparseable cgroup content as
// "no useful answer here" and falls through to /proc/meminfo. Defensive — a
// future cgroup v3 might write something we don't recognize.
func TestMemoryBytesFrom_CgroupParseError(t *testing.T) {
	dir := t.TempDir()
	cgroup := filepath.Join(dir, "memory.max")
	meminfo := filepath.Join(dir, "meminfo")
	if err := os.WriteFile(cgroup, []byte("garbage\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(meminfo, []byte("MemTotal:       4096 kB\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got := memoryBytesFrom(cgroup, meminfo)
	if got != 4096*1024 {
		t.Errorf("memoryBytesFrom: want %d (meminfo fallback), got %d", 4096*1024, got)
	}
}

// TestParseMeminfoMemTotal_MissingField returns 0 when MemTotal isn't present.
func TestParseMeminfoMemTotal_MissingField(t *testing.T) {
	got := parseMeminfoMemTotal([]byte("MemFree: 1024 kB\n"))
	if got != 0 {
		t.Errorf("parseMeminfoMemTotal: want 0, got %d", got)
	}
}
