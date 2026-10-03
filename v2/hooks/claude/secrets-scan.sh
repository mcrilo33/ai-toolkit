#!/usr/bin/env bash
# secrets-scan: PreToolUse(Write|Edit|MultiEdit|NotebookEdit). Blocks writing a hardcoded secret (same pattern set as
# v1 shared/hooks/lib/utils.sh). Exit 2 + stderr = deny; a crash (bad JSON, no jq) is exit 2 too (fail-closed).
set -euo pipefail
deny() { echo "secrets-scan: blocked: $*" >&2; exit 2; }
trap 'deny "cannot parse the tool payload (fail-closed)"' ERR
re='sk-[a-zA-Z0-9]{20,}|sk-proj-[a-zA-Z0-9_-]{20,}|sk-lf-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|gh[pos]_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{22,}'
re="$re|glpat-[a-zA-Z0-9_-]{20,}|xox[bp]-[0-9]{10,}-[a-zA-Z0-9]{20,}|[sp]k_live_[a-zA-Z0-9]{24,}|sq0csp-[a-zA-Z0-9_-]{40,}"
re="$re|AIza[0-9A-Za-z_-]{35}|ya29\.[0-9A-Za-z_-]+|eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}|npm_[a-zA-Z0-9]{36}"
re="$re|pypi-AgEIcH[a-zA-Z0-9_-]{50,}|SG\.[a-zA-Z0-9_-]{22}\.[a-zA-Z0-9_-]{43}|key-[a-zA-Z0-9]{32}|LANGFUSE_BASIC_AUTH[^B]{0,4}Basic [A-Za-z0-9+/=]{20,}"
txt="$(jq -r '[.tool_input | (.content, .new_string, .new_source, (.edits[]?.new_string))] | map(select(. != null)) | join("\n")' <<<"$(cat)")"
if grep -qE "$re" <<<"$txt"; then deny "a hardcoded secret (API key/token) is in the content being written: use an environment variable"; fi
exit 0
