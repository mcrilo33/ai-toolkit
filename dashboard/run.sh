#!/usr/bin/env bash
# Launch the workflow-observability dashboard (Issue #23).
#
# 100% local: Streamlit binds to localhost only and the dashboard reads a local
# span log. Nothing is exported. See README.md.
#
# Usage:
#   dashboard/run.sh                 # read the default span log
#   AI_TOOLKIT_TELEMETRY_DIR=… dashboard/run.sh
#   dashboard/run.sh path/to/events.jsonl
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Optional first arg: an explicit span-log path, surfaced to app.py via env.
if [ "${1:-}" != "" ]; then
  export AI_TOOLKIT_SPAN_LOG="$1"
fi

exec streamlit run "$HERE/app.py" \
  --server.address localhost \
  --browser.gatherUsageStats false
