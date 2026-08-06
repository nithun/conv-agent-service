#!/usr/bin/env bash
# Regenerate the OpenWiki wiki for tap-agent-service.
# Portable: no Claude Code required. Safe to run by hand or from a git hook / CI.
#
# One-time setup (per machine):
#   npm install -g openwiki
#   mkdir -p ~/.openwiki && printf 'ANTHROPIC_API_KEY=sk-...\n' >> ~/.openwiki/.env
#   (cd <repo> && openwiki --init)   # keep the pre-seeded openwiki/INSTRUCTIONS.md
#
# Then: scripts/openwiki-refresh.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v openwiki >/dev/null 2>&1; then
  echo "openwiki not found. Install it first:  npm install -g openwiki" >&2
  echo "(then add ANTHROPIC_API_KEY to ~/.openwiki/.env)" >&2
  exit 127
fi

echo "==> Refreshing OpenWiki for $(basename "$REPO_ROOT") ..."
openwiki --update

echo "==> Wiki changes (openwiki/):"
git status --porcelain openwiki/ || true
echo "==> Review the diff, then commit openwiki/ yourself. This script never commits."
