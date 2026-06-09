#!/usr/bin/env bash
# secrets-scan — block hardcoded secrets (API keys, tokens).
#
# Two entry points, one script (shared across platforms):
#
#   • Cursor (beforeShellExecution): on `git add` / `git commit`, scan the
#     STAGED content (git diff --cached) for secret patterns and DENY the
#     command. This is the real enforcement point on Cursor — the generic
#     pre-write Write path delivers an internal scratch payload, so a pre-write
#     block there is unreliable. afterFileEdit early containment is handled by
#     the sibling secrets-scan-revert.sh.
#
#   • Claude/Copilot (preToolUse / Write|Edit): scan the content being written
#     (tool_input.content / .new_string) and DENY before the write lands.
#
# Exit 2 = block, Exit 0 = allow.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
EVENT=$(get_hook_event "$INPUT")

# ── Cursor dedicated event: commit-time staged-content scan ─────────
if [ "$EVENT" = "beforeShellExecution" ]; then
  COMMAND=$(get_shell_command "$INPUT")
  [ -z "$COMMAND" ] && exit 0

  # Only act on commands that move content into the commit (add/commit).
  # Match anywhere in the command so chained/prefixed forms are not bypassed
  # (e.g. `cd sub && git add`, `git -C path commit`, `… ; git add -A`).
  is_git_commit_or_add "$COMMAND" || exit 0

  PROJECT_ROOT=$(project_root_from_payload "$INPUT")

  # Staged content = the index diff. ACMR skips deletions (nothing to scan).
  # --text forces a textual diff even for files git would treat as binary, so a
  # secret cannot hide behind a "Binary files differ" suppression.
  STAGED_DIFF=$(git -C "$PROJECT_ROOT" diff --cached --text --diff-filter=ACMR 2>/dev/null || true)
  [ -z "$STAGED_DIFF" ] && exit 0

  # Only consider added lines (leading '+', excluding the +++ file header).
  ADDED=$(printf '%s\n' "$STAGED_DIFF" | grep -E '^\+' | grep -vE '^\+\+\+' || true)
  [ -z "$ADDED" ] && exit 0

  if FOUND=$(scan_for_secret "$ADDED"); then
    deny "Secret detected in staged content: $FOUND.
Remove the secret and use environment variables instead, then re-stage.
Staged content was scanned via 'git diff --cached'."
  fi
  exit 0
fi

# ── Claude/Copilot: pre-write content scan ──────────────────────────
CONTENT=$(get_edit_new_content "$INPUT")
[ -z "$CONTENT" ] && exit 0

if FOUND=$(scan_for_secret "$CONTENT"); then
  FILE_PATH=$(get_edit_file_path "$INPUT")
  deny "Secret detected: $FOUND in ${FILE_PATH:-file}. Use environment variables instead of hardcoding secrets."
fi

exit 0
