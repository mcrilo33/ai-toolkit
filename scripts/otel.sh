#!/usr/bin/env bash
# Native-OTel collector lifecycle: otel.sh up|down|status (docker compose, langfuse/compose.yaml).
# `up` needs LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY (or LANGFUSE_BASIC_AUTH) in the caller env, else the
# macOS Keychain (services ai-toolkit-langfuse-LANGFUSE_INIT_PROJECT_{PUBLIC,SECRET}_KEY); an env file is never read
# for them. The credential is checked against Langfuse first, then reaches docker through its environment, never argv.
# `status` exits 1 when the collector is down and 3 when it is up but failing to export to Langfuse.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
. "$here/lib.sh"
auth="${LANGFUSE_BASIC_AUTH:-}" pk="${LANGFUSE_PUBLIC_KEY:-}" sk="${LANGFUSE_SECRET_KEY:-}"  # the caller env only
unset LANGFUSE_BASIC_AUTH LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY
load_env
[ -z "${LANGFUSE_BASIC_AUTH:-}${LANGFUSE_SECRET_KEY:-}" ] ||
  echo "otel.sh: ignoring LANGFUSE_BASIC_AUTH/LANGFUSE_SECRET_KEY found in an env file: move the key pair to the Keychain" >&2
keychain() { security find-generic-password -a "${USER:-$(id -un)}" -s "ai-toolkit-langfuse-LANGFUSE_INIT_PROJECT_$1_KEY" -w 2> /dev/null || true; }
dc() { docker compose -p ai-toolkit-otel -f "${OTEL_COMPOSE_FILE:-$here/../langfuse/compose.yaml}" "$@"; }
export AITK_OTEL_RAW_DIR="${AITK_OTEL_RAW_DIR:-$HOME/.ai-toolkit/otel-raw}"

case "${1:-}" in
  up)
    if [ -z "$auth" ]; then
      [ -n "$pk" ] && [ -n "$sk" ] || { pk="$(keychain PUBLIC)"; sk="$(keychain SECRET)"; }
      [ -n "$pk" ] && [ -n "$sk" ] ||
        die "no Langfuse key pair: export LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY, or store them in the Keychain (security add-generic-password -a \"\$USER\" -s ai-toolkit-langfuse-LANGFUSE_INIT_PROJECT_{PUBLIC,SECRET}_KEY -w)"
      auth="Basic $(printf '%s:%s' "$pk" "$sk" | base64 | tr -d '\n')"
    fi
    lf_host="${LANGFUSE_HOST:-http://localhost:3000}"
    host="${lf_host/localhost/host.docker.internal}"
    export LANGFUSE_BASIC_AUTH="$auth" LANGFUSE_OTLP_ENDPOINT="${LANGFUSE_OTLP_ENDPOINT:-${host/127.0.0.1/host.docker.internal}/api/public/otel}"
    d="$AITK_OTEL_RAW_DIR"; while [ ! -d "$d" ]; do d="$(dirname "$d")"; done  # nearest existing ancestor
    if [ "$(env -u GIT_DIR -u GIT_WORK_TREE git -C "$d" rev-parse --is-inside-work-tree 2> /dev/null)" = true ]; then
      die "AITK_OTEL_RAW_DIR must not be inside a git worktree: $AITK_OTEL_RAW_DIR"
    fi
    # a rejected credential would drop every export silently (a 401 is not retried), so stop before the stack starts
    code="$(printf 'header = "Authorization: %s"\n' "$auth" | curl -sS -m 5 -K - -o /dev/null -w '%{http_code}' "$lf_host/api/public/projects" 2> /dev/null)" || code=000
    case "$code" in
      2??) ;;
      000) echo "otel.sh: Langfuse unreachable at $lf_host, starting without checking the credential" >&2 ;;
      *) die "Langfuse rejected the credential (HTTP $code)" ;;
    esac
    mkdir -p "$AITK_OTEL_RAW_DIR" && chmod 700 "$AITK_OTEL_RAW_DIR"  # holds prompts and tool output in plaintext
    dc up -d ;;
  down) dc down ;;
  status)
    [ -n "$(dc ps --status running --quiet 2> /dev/null || true)" ] || { echo down; exit 1; }
    fails="$(LC_ALL=C docker logs --since 15m lf-collector 2>&1 | grep 'Exporting failed.*otlp_http/langfuse' || true)"
    [ -n "$fails" ] || { echo up; exit 0; }
    last="$(printf '%s\n' "$fails" | tail -1 | sed -nE 's/.*HTTP Status Code ([0-9]+).*/\1/p')"
    echo "degraded: $(printf '%s\n' "$fails" | wc -l | tr -d ' ') export failures in 15m (last: ${last:-no HTTP status})"; exit 3 ;;
  *) echo "usage: ${0##*/} up|down|status" >&2; exit 2 ;;
esac
