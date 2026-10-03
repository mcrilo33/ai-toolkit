#!/usr/bin/env bash
# Native-OTel collector lifecycle: otel.sh up|down|status (docker compose, langfuse/compose.yaml).
# `up` needs LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY (or LANGFUSE_BASIC_AUTH) in the caller env or
# .ai-toolkit/ai-toolkit.local.env; they reach docker through its environment, never its argv.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
. "$here/lib.sh"
load_env
dc() { docker compose -p ai-toolkit-otel -f "${OTEL_COMPOSE_FILE:-$here/../langfuse/compose.yaml}" "$@"; }
export AITK_OTEL_RAW_DIR="${AITK_OTEL_RAW_DIR:-$HOME/.ai-toolkit/otel-raw}"

case "${1:-}" in
  up)
    if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
      [ -n "${LANGFUSE_PUBLIC_KEY:-}" ] && [ -n "${LANGFUSE_SECRET_KEY:-}" ] ||
        die "set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .ai-toolkit/ai-toolkit.local.env"
      LANGFUSE_BASIC_AUTH="Basic $(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64 | tr -d '\n')"
    fi
    host="${LANGFUSE_HOST:-http://localhost:3000}"
    export LANGFUSE_BASIC_AUTH LANGFUSE_OTLP_ENDPOINT="${LANGFUSE_OTLP_ENDPOINT:-${host/localhost/host.docker.internal}/api/public/otel}"
    mkdir -p "$AITK_OTEL_RAW_DIR"
    if git -C "$AITK_OTEL_RAW_DIR" rev-parse --git-dir > /dev/null 2>&1; then
      die "AITK_OTEL_RAW_DIR must not be inside a git worktree: $AITK_OTEL_RAW_DIR"
    fi
    dc up -d ;;
  down) dc down ;;
  status)
    if [ -n "$(dc ps --status running --quiet 2> /dev/null || true)" ]; then echo up; else echo down; exit 1; fi ;;
  *) echo "usage: ${0##*/} up|down|status" >&2; exit 2 ;;
esac
