#!/usr/bin/env bash
# Launch the workflow-observability dashboard (Issue #23).
#
# 100% local: Streamlit binds to localhost only and the dashboard reads a local
# span log. Nothing is exported. See README.md.
#
# Port: picks the first FREE port in a dedicated range (default 8600-8699) so it
# never lands on hex admin's Streamlit on the default 8501 — a collision there
# makes the browser show the wrong app. Override the range with
# AI_TOOLKIT_DASHBOARD_PORT_MIN / _MAX, or pin one port with
# STREAMLIT_SERVER_PORT (which wins over the range). If the whole range is busy,
# fall back to a random free ephemeral port and print a note.
#
# Usage:
#   dashboard/run.sh                 # read the default span log
#   AI_TOOLKIT_TELEMETRY_DIR=… dashboard/run.sh
#   dashboard/run.sh path/to/events.jsonl
#   STREAMLIT_SERVER_PORT=8700 dashboard/run.sh           # pin a port
#   AI_TOOLKIT_DASHBOARD_PORT_MIN=8700 AI_TOOLKIT_DASHBOARD_PORT_MAX=8799 dashboard/run.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Optional first arg: an explicit span-log path, surfaced to app.py via env.
if [ "${1:-}" != "" ]; then
  export AI_TOOLKIT_SPAN_LOG="$1"
fi

PORT_MIN="${AI_TOOLKIT_DASHBOARD_PORT_MIN:-8600}"
PORT_MAX="${AI_TOOLKIT_DASHBOARD_PORT_MAX:-8699}"

# Honour an explicit STREAMLIT_SERVER_PORT; else scan the dedicated range for the
# first free port. Exit 3 (and an ephemeral port) signals the range was full.
if [ -n "${STREAMLIT_SERVER_PORT:-}" ]; then
  PORT="$STREAMLIT_SERVER_PORT"
else
  # A free port is bound then immediately closed before handing it to streamlit,
  # so there is a tiny TOCTOU race — fine for local dev.
  set +e
  PORT="$(python3 - "$PORT_MIN" "$PORT_MAX" <<'PY'
import socket
import sys

lo, hi = int(sys.argv[1]), int(sys.argv[2])
for port in range(lo, hi + 1):
    s = socket.socket()
    try:
        s.bind(("localhost", port))
    except OSError:
        continue
    finally:
        s.close()
    print(port)
    sys.exit(0)

# Whole range busy: fall back to a random free ephemeral port.
s = socket.socket()
s.bind(("localhost", 0))
print(s.getsockname()[1])
s.close()
sys.exit(3)
PY
)"
  picked=$?
  set -e
  # Exit 0 = a port in range; 3 = range full (PORT is the ephemeral fallback);
  # anything else (e.g. a non-integer MIN/MAX) is a hard error — never launch
  # streamlit with an empty port.
  if [ "$picked" -ne 0 ] && [ "$picked" -ne 3 ]; then
    echo "error: could not pick a port; AI_TOOLKIT_DASHBOARD_PORT_MIN/_MAX must be integers" >&2
    exit 1
  fi
  if [ "$picked" -eq 3 ]; then
    echo "note: dashboard port range $PORT_MIN-$PORT_MAX is fully in use; falling back to ephemeral port $PORT" >&2
  fi
fi

echo "Dashboard: http://localhost:$PORT" >&2

exec streamlit run "$HERE/app.py" \
  --server.address localhost \
  --server.port "$PORT" \
  --browser.gatherUsageStats false
