#!/usr/bin/env node
/**
 * Dependency-cruiser enforcement gate.
 *
 * Runs the full boundary/barrel rule set from .dependency-cruiser.cjs and
 * exits non-zero if any violation is found. Uses the programmatic API
 * directly so the Node version check in the depcruise CLI does not block
 * Node 23.x (supported by the runtime; just not yet in depcruise's engine
 * semver range).
 */
import { cruise, format } from "dependency-cruiser";
import { createRequire } from "module";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

const config = require(join(__dirname, "../.dependency-cruiser.cjs"));

const result = await cruise(["src"], {
  ruleSet: { forbidden: config.forbidden },
  ...config.options,
});

const violations = result.output.modules.flatMap((m) =>
  m.dependencies
    .filter((d) => d.rules && d.rules.length > 0)
    .map((d) => ({ from: m.source, to: d.resolved, rules: d.rules })),
);

if (violations.length === 0) {
  console.log("  dependency-cruiser: 0 violations ✓");
  process.exit(0);
}

console.error(`  dependency-cruiser: ${violations.length} violation(s) found:`);
for (const v of violations) {
  for (const r of v.rules) {
    console.error(`    [${r.name}] ${v.from} → ${v.to}`);
  }
}
process.exit(1);
