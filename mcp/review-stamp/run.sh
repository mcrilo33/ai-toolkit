#!/usr/bin/env bash
# review-stamp MCP server launcher.
#
# Resolves REVIEW_STAMP_KEY — env first, else the macOS Keychain item
# REVIEW_STAMP_KEY — and execs the stdlib-Python server with the key in its
# environment. The key only ever lives in process memory: it is never written
# to disk, logged, or echoed.
set -euo pipefail

if [ -z "${REVIEW_STAMP_KEY:-}" ] && command -v security &>/dev/null; then
  REVIEW_STAMP_KEY=$(security find-generic-password -a "$USER" -s REVIEW_STAMP_KEY -w 2>/dev/null || true)
  export REVIEW_STAMP_KEY
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/server.py"
