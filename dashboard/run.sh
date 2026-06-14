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

# Pick a free port so we never collide with another Streamlit on the default
# 8501. Honour STREAMLIT_SERVER_PORT if the caller set one; else grab a free
# ephemeral port (a tiny race window, fine for local dev).
PORT="${STREAMLIT_SERVER_PORT:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("localhost", 0)); print(s.getsockname()[1]); s.close()')}"

exec streamlit run "$HERE/app.py" \
  --server.address localhost \
  --server.port "$PORT" \
  --browser.gatherUsageStats false
