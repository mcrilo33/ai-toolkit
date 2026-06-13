#!/usr/bin/env bash
# telemetry-report — summarize the opt-in telemetry span log.
# Usage: telemetry-report.sh [events.jsonl]
# Default file: ${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl
#
# The log holds unified spans (kind/name/status/phase/…). For backward
# compatibility it also reads any legacy {ts,hook,decision,repo} lines a
# pre-migration log may still contain: name falls back to .hook, status to
# .decision, kind to "hook".
set -euo pipefail

EVENTS_FILE="${1:-${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl}"

if [ ! -f "$EVENTS_FILE" ]; then
  echo "No telemetry events recorded."
  exit 0
fi

# Reduce each event to "<kind>\t<name>\t<status>\t<phase>", one per line. The
# span model is preferred; .hook/.decision are the legacy fallbacks (no phase).
if command -v jq &>/dev/null; then
  ROWS=$(jq -r \
    '[(.kind // "hook"), (.name // .hook // "?"), (.status // .decision // "?"), (.phase // "")] | @tsv' \
    "$EVENTS_FILE")
else
  # Best-effort jq-less extraction: prefer span keys, fall back to legacy ones.
  # Phase is left empty here (the per-phase section needs jq); name/kind/status
  # — the primary summary — still work.
  ROWS=$(sed -n \
    's/.*"kind":"\([^"]*\)".*"name":"\([^"]*\)".*"status":"\([^"]*\)".*/\1\t\2\t\3\t/p;
     s/.*"hook":"\([^"]*\)".*"decision":"\([^"]*\)".*/hook\t\1\t\2\t/p' \
    "$EVENTS_FILE")
fi

echo "Telemetry report — $EVENTS_FILE"
echo
echo "Per name:"
printf '%s\n' "$ROWS" | awk -F'\t' 'NF { print $2"\t"$3 }' | sort | uniq -c \
  | awk '{ printf "  %-30s %-8s %d\n", $2, $3, $1 }'
echo
echo "By kind:"
printf '%s\n' "$ROWS" | awk -F'\t' '
  NF { kind[$1]++ }
  END { for (k in kind) printf "  %-10s %d\n", k, kind[k] }'

# Cycle phases live only on step spans (phase = red/green/review/push); surface
# them so the cycle-semantic view is not collapsed into the constant name.
PHASE_ROWS=$(printf '%s\n' "$ROWS" | awk -F'\t' '$1 == "step" && $4 != ""')
if [ -n "$PHASE_ROWS" ]; then
  echo
  echo "Per cycle phase:"
  printf '%s\n' "$PHASE_ROWS" | awk -F'\t' '{ print $4"\t"$3 }' | sort | uniq -c \
    | awk '{ printf "  %-10s %-8s %d\n", $2, $3, $1 }'
fi
echo
echo "Totals:"
printf '%s\n' "$ROWS" | awk -F'\t' '
  NF { status[$3]++; n++ }
  END {
    for (s in status) printf "  %-8s %d\n", s, status[s]
    printf "  events %d\n", n
  }'
