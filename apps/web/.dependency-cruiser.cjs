/** @type {import('dependency-cruiser').IConfiguration} */
module.exports = {
  forbidden: [
    // ── Layer / domain direction ────────────────────────────────────────────
    // core/* must not import from domain/*.
    {
      name: "core-no-domain",
      severity: "error",
      from: { path: "^src/core" },
      to: { path: "^src/domain" },
      comment: "core must not depend on domain",
    },
    // shared/* must not import from core/* or domain/*.
    // shared/components/ui/ is excluded (managed vendor layer; arbitrary
    // internal imports are intentional there).
    {
      name: "shared-no-core-domain",
      severity: "error",
      from: {
        path: "^src/shared",
        pathNot: "^src/shared/components/ui",
      },
      to: { path: "^src/(core|domain)" },
      comment: "shared must not depend on core or domain",
    },
    // domain/X must not import from domain/Y (different domain modules).
    // Mechanism: capture the importer's first path segment as $1 and forbid
    // imports whose target starts with src/domain/ but differs from $1.
    {
      name: "no-cross-domain",
      severity: "error",
      from: { path: "^src/domain/([^/]+)" },
      to: {
        path: "^src/domain",
        pathNot: "^src/domain/$1(/|$)",
      },
      comment: "domain modules must not import each other",
    },
    // Only core/api/* may import from core/api/generated/*.
    {
      name: "generated-encapsulated",
      severity: "error",
      from: { pathNot: "^src/core/api(/|$)" },
      to: { path: "^src/core/api/generated" },
      comment: "only core/api may import generated types",
    },
    // ── Barrel-only encapsulation ───────────────────────────────────────────
    // Cross-module imports must resolve to the target module's index.ts(x).
    // Deep imports into another module's internals are forbidden.
    // Intra-module deep imports are always legal (^$1/ exclusion).
    // shared/components/ui/ is excluded (managed vendor layer).
    {
      name: "barrel-only",
      severity: "error",
      from: {
        path: "^src/(core|domain|shared)/([^/]+)",
        pathNot: "^src/shared/components/ui",
      },
      to: {
        path: "^src/(core|domain|shared)/([^/]+)/",
        pathNot: [
          // The target is the importer's own module — intra-module deep imports are legal.
          "^src/(core|domain|shared)/$2/",
          // The target is an index barrel — barrel import is legal.
          "/index\\.tsx?$",
          // Excluded vendor layer.
          "^src/shared/components/ui/",
        ],
      },
      comment:
        "cross-module imports must resolve to index.ts(x) barrels; deep imports into another module's internals are forbidden",
    },
  ],

  options: {
    doNotFollow: {
      path: ["node_modules", "src/core/api/generated", "src/shared/components/ui"],
    },
    moduleSystems: ["es6", "cjs"],
    tsPreCompilationDeps: true,
    externalModuleResolutionStrategy: "node_modules",
    tsConfig: {
      fileName: "tsconfig.json",
    },
    // Map path aliases declared in tsconfig.json.
    paths: {
      "@core/*": ["src/core/*"],
      "@domain/*": ["src/domain/*"],
      "@shared/*": ["src/shared/*"],
    },
    reporterOptions: {
      text: {
        highlightFocused: true,
      },
    },
  },
};
