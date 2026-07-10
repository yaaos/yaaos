#!/usr/bin/env node
// Build apps/web/public/yaaos-pipeline-skills.zip — a zip of every
// `pipeline-*` skill (.claude/skills/pipeline-*/**) and `pipeline-*`
// agent (.claude/agents/pipeline-*.md).
//
// Users run their own repos through the yaaos agent, which stats
// `.claude/skills/<skill>/SKILL.md` inside the user's checkout before
// spawning Claude Code. This script ships the shipped pipeline skills +
// agents as a static download so "unzip at the repo root" is the whole
// install instruction — every entry path is repo-root-relative
// (`.claude/...`).
//
// Selection is purely the `pipeline-` prefix — `dev-*`, `yaaos-*`, and
// `rwx` skills are yaaos-internal and must never ship here.
//
// Runs before `pnpm build` / `pnpm dev` (prebuild/predev hooks in
// package.json) and is also invoked directly by bin/ci, which calls
// `vite build` rather than the `build` package script.

import { existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { zipSync } from "fflate";

function fail(message) {
  console.error(`build-skills-bundle: ERROR: ${message}`);
  process.exit(1);
}

// Resolve the repo root by walking up from this script's own location
// until a directory containing `.claude/skills` is found — never a
// hardcoded absolute path, so the script works from any checkout.
function findRepoRoot(startDir) {
  let dir = startDir;
  for (;;) {
    if (existsSync(join(dir, ".claude", "skills"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) {
      fail(`could not locate repo root (no .claude/skills found walking up from ${startDir})`);
    }
    dir = parent;
  }
}

function collectFilesRecursive(dir) {
  const out = [];
  const entries = readdirSync(dir, { withFileTypes: true }).sort((a, b) =>
    a.name.localeCompare(b.name),
  );
  for (const entry of entries) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...collectFilesRecursive(full));
    } else if (entry.isFile()) {
      out.push(full);
    }
  }
  return out;
}

function toArchivePath(repoRoot, absPath) {
  return relative(repoRoot, absPath).split(sep).join("/");
}

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = findRepoRoot(scriptDir);
const skillsDir = join(repoRoot, ".claude", "skills");
const agentsDir = join(repoRoot, ".claude", "agents");
const outPath = join(repoRoot, "apps", "web", "public", "yaaos-pipeline-skills.zip");

// --- Collect pipeline-* skill directories. ---
const skillDirNames = readdirSync(skillsDir, { withFileTypes: true })
  .filter((e) => e.isDirectory() && e.name.startsWith("pipeline-"))
  .map((e) => e.name)
  .sort();

if (skillDirNames.length === 0) {
  fail(`no pipeline-* skill directories found under ${skillsDir}`);
}

/** @type {Record<string, Uint8Array>} */
const files = {};

for (const name of skillDirNames) {
  const dir = join(skillsDir, name);
  const dirFiles = collectFilesRecursive(dir);
  const hasSkillMd = dirFiles.some((f) => f.endsWith(`${sep}SKILL.md`));
  const hasSchemaJson = dirFiles.some((f) => f.endsWith(".schema.json"));
  // pipeline-schemas ships only *.schema.json files (no SKILL.md) — the
  // engine-injected schema is authoritative at runtime; these are the
  // hand-maintained copies for local/human skill runs. Every other
  // pipeline-* skill must carry a SKILL.md.
  const isValid = hasSkillMd || (name === "pipeline-schemas" && hasSchemaJson);
  if (!isValid) {
    fail(`skill directory ${name} is missing SKILL.md`);
  }
  for (const file of dirFiles) {
    files[toArchivePath(repoRoot, file)] = readFileSync(file);
  }
}

// --- Collect pipeline-*.md agent files. ---
const agentFileNames = readdirSync(agentsDir, { withFileTypes: true })
  .filter((e) => e.isFile() && e.name.startsWith("pipeline-") && e.name.endsWith(".md"))
  .map((e) => e.name)
  .sort();

if (agentFileNames.length === 0) {
  fail(`no pipeline-*.md agent files found under ${agentsDir}`);
}

for (const name of agentFileNames) {
  const full = join(agentsDir, name);
  files[toArchivePath(repoRoot, full)] = readFileSync(full);
}

// Deterministic (sorted) entry ordering so the artifact is reproducible.
/** @type {Record<string, Uint8Array>} */
const sortedFiles = {};
for (const key of Object.keys(files).sort()) {
  sortedFiles[key] = files[key];
}

// Fixed mtime on every entry — otherwise fflate stamps the build-time
// wall clock into each entry, making the archive bytes differ run to run
// even when the source content hasn't changed. The DOS date format used
// by the ZIP spec only represents 1980-2099, so epoch (1970) isn't valid.
const FIXED_MTIME = new Date("2020-01-01T00:00:00Z");
const zipped = zipSync(sortedFiles, { level: 9, mtime: FIXED_MTIME });

writeFileSync(outPath, zipped);

console.log(
  `build-skills-bundle: wrote ${outPath} (${Object.keys(sortedFiles).length} entries, ${zipped.length} bytes)`,
);
