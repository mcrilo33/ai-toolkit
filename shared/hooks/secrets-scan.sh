#!/usr/bin/env bash
# secrets-scan — PreToolUse hook
# Scans file content being written for hardcoded secrets, API keys, and tokens.
# Blocks the write if a secret pattern is detected.
#
# Exit 2 = block
# Exit 0 = allow
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
TOOL_INPUT=$(get_tool_input "$INPUT")

# Extract content being written (Write tool) or new_string (Edit tool)
CONTENT=""
if command -v jq &>/dev/null; then
  CONTENT=$(echo "$TOOL_INPUT" | jq -r '.content // .new_string // empty' 2>/dev/null)
fi

[ -z "$CONTENT" ] && exit 0

# ── Secret patterns ─────────────────────────────────────────────────
# Each pattern: regex<TAB>description
PATTERNS=(
  'sk-[a-zA-Z0-9]{20,}	OpenAI API key'
  'sk-proj-[a-zA-Z0-9_-]{20,}	OpenAI project key'
  'AKIA[0-9A-Z]{16}	AWS Access Key ID'
  'ghp_[a-zA-Z0-9]{36}	GitHub personal access token'
  'gho_[a-zA-Z0-9]{36}	GitHub OAuth token'
  'ghs_[a-zA-Z0-9]{36}	GitHub server token'
  'github_pat_[a-zA-Z0-9_]{22,}	GitHub fine-grained PAT'
  'glpat-[a-zA-Z0-9_-]{20,}	GitLab personal access token'
  'xoxb-[0-9]{10,}-[a-zA-Z0-9]{20,}	Slack bot token'
  'xoxp-[0-9]{10,}-[a-zA-Z0-9]{20,}	Slack user token'
  'sk_live_[a-zA-Z0-9]{24,}	Stripe secret key'
  'pk_live_[a-zA-Z0-9]{24,}	Stripe publishable key'
  'sq0csp-[a-zA-Z0-9_-]{40,}	Square credential'
  'AIza[0-9A-Za-z_-]{35}	Google API key'
  'ya29\.[0-9A-Za-z_-]+	Google OAuth token'
  'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}	JWT token (if long)'
  'npm_[a-zA-Z0-9]{36}	npm access token'
  'pypi-AgEIcH[a-zA-Z0-9_-]{50,}	PyPI API token'
  'SG\.[a-zA-Z0-9_-]{22}\.[a-zA-Z0-9_-]{43}	SendGrid API key'
  'key-[a-zA-Z0-9]{32}	Mailgun API key'
)

FOUND=""
for entry in "${PATTERNS[@]}"; do
  PATTERN="${entry%%	*}"
  DESC="${entry#*	}"
  if echo "$CONTENT" | grep -qE "$PATTERN"; then
    FOUND="$DESC"
    break
  fi
done

if [ -n "$FOUND" ]; then
  FILE_PATH=$(get_file_path "$INPUT")
  deny "Secret detected: $FOUND in ${FILE_PATH:-file}. Use environment variables instead of hardcoding secrets."
fi

exit 0
