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

# Fail closed on an unparseable payload (issue #208). jq choking on malformed or
# shape-mismatched JSON exits non-zero (5); under `set -euo pipefail` that
# propagates out of an extraction assignment and would exit the hook with jq's
# code. Claude Code treats any exit other than 2 as a NON-blocking error, so the
# guarded write would proceed with its content unscanned. This ERR trap converts
# any uncaught crash below into a deny (exit 2) — a malformed payload blocks
# instead of silently passing.
trap 'deny "secrets-scan could not parse the tool payload to scan for secrets; blocking the write (fail-closed, issue #208)."' ERR

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
# Content is extracted only via jq (get_edit_new_content is jq-only), so with jq
# absent the scanner is blind and an empty CONTENT is indistinguishable from a
# genuinely clean write. A blind secrets guard must fail closed rather than pass
# an unscanned write (issue #208). The Cursor commit-time path above uses
# git-diff + grep and does not need jq, so this gate is confined to this path.
if ! command -v jq >/dev/null 2>&1; then
  deny "secrets-scan cannot inspect content for hardcoded secrets: jq is unavailable. Install jq — the scanner fails closed rather than pass an unscanned write (issue #208)."
fi

CONTENT=$(get_edit_new_content "$INPUT")
[ -z "$CONTENT" ] && exit 0

if FOUND=$(scan_for_secret "$CONTENT"); then
  FILE_PATH=$(get_edit_file_path "$INPUT")
  deny "Secret detected: $FOUND in ${FILE_PATH:-file}. Use environment variables instead of hardcoding secrets."
fi

exit 0
