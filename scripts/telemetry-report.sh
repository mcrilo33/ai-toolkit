#!/usr/bin/env bash
# telemetry-report — summarize the opt-in hook telemetry event log.
# Usage: telemetry-report.sh [events.jsonl]
# Default file: ${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl
set -euo pipefail

EVENTS_FILE="${1:-${AI_TOOLKIT_TELEMETRY_DIR:-$HOME/.ai-toolkit/telemetry}/events.jsonl}"

if [ ! -f "$EVENTS_FILE" ]; then
  echo "No telemetry events recorded."
  exit 0
fi

# Extract "<hook>\t<decision>" pairs, one per event.
if command -v jq &>/dev/null; then
  PAIRS=$(jq -r '[.hook, .decision] | @tsv' "$EVENTS_FILE")
else
  PAIRS=$(sed -n 's/.*"hook":"\([^"]*\)".*"decision":"\([^"]*\)".*/\1\t\2/p' "$EVENTS_FILE")
fi

echo "Telemetry report — $EVENTS_FILE"
echo
echo "Per hook:"
printf '%s\n' "$PAIRS" | sort | uniq -c \
  | awk '{ printf "  %-30s %-6s %d\n", $2, $3, $1 }'
echo
echo "Totals:"
printf '%s\n' "$PAIRS" | awk -F'\t' '
  { total[$2]++; n++ }
  END {
    for (d in total) printf "  %-6s %d\n", d, total[d]
    printf "  events %d\n", n
  }'
