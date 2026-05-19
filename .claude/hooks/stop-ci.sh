#!/usr/bin/env bash
# Stop hook — gates per-app `bin/ci` based on what changed since HEAD.
#
# Fires when Claude is about to end a turn. Scopes:
#   - Pure-doc changes (*.md only) → skip (no CI run, instant).
#   - Anything under apps/backend/ (non-.md) → run apps/backend/bin/ci.
#   - Anything under apps/web/ (non-.md) → run apps/web/bin/ci.
#   - Infra-only edits (bin/, docker/, .gitignore, .env.sample, .claude/) → skip.
#
# Exits non-zero on CI failure to block the turn from ending; output is
# surfaced back to Claude. e2e is NOT in this hook — it's expensive and
# Docker-dependent; the discipline rule in CLAUDE.md covers when to run it.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# All paths changed since HEAD: working-tree modifications + untracked,
# de-duplicated. Ignores files outside the index that .gitignore covers.
CHANGED=$(
  {
    git diff --name-only HEAD 2>/dev/null
    git ls-files --others --exclude-standard 2>/dev/null
  } | sort -u
)

if [ -z "$CHANGED" ]; then
  exit 0
fi

needs_backend=0
needs_web=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  # Pure-doc / plan / infra changes don't need code CI.
  case "$f" in
    *.md) continue ;;
    plan/*|docs/*) continue ;;
    .claude/*|.gitignore|.env.sample|bin/*|docker/*) continue ;;
  esac
  case "$f" in
    apps/backend/*) needs_backend=1 ;;
    apps/web/*) needs_web=1 ;;
  esac
done <<<"$CHANGED"

failed=0
if [ "$needs_backend" = "1" ]; then
  echo "[stop-ci] backend code changed → apps/backend/bin/ci" >&2
  if ! (cd apps/backend && bin/ci) >&2; then
    failed=1
  fi
fi
if [ "$needs_web" = "1" ]; then
  echo "[stop-ci] web code changed → apps/web/bin/ci" >&2
  if ! (cd apps/web && bin/ci) >&2; then
    failed=1
  fi
fi

if [ "$failed" = "1" ]; then
  echo "[stop-ci] one or more bin/ci scripts failed — fix before ending the turn" >&2
  exit 2
fi

exit 0
