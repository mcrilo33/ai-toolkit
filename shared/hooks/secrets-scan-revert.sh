#!/usr/bin/env bash
# secrets-scan-revert — afterFileEdit early containment for hardcoded secrets.
#
# Cursor's afterFileEdit cannot block and has NO agent-visible output channel,
# so it cannot warn or deny. What it CAN do is CONTAIN: if an edit just wrote a
# secret to disk, neutralize the secret immediately to limit exposure. The
# authoritative deny happens later at commit time (secrets-scan.sh on
# beforeShellExecution); this is defense-in-depth, not the primary gate.
#
# SAFETY (data-loss is unacceptable — see security review):
#   • A timestamped backup of the file is written BEFORE any mutation, so the
#     user can always recover. The backup path is logged.
#   • Containment is SURGICAL: only the line(s) that actually contain a secret
#     pattern are removed. Unrelated content is never touched.
#   • We never run a blind `git checkout`/`rm` that could discard legitimate
#     uncommitted work. If surgical redaction cannot make the file clean, we
#     leave the file as-is (backed up) and log loudly — the commit-time gate is
#     the backstop.
#
# Only runs on Cursor's afterFileEdit. On any other event (Claude/Copilot
# postToolUse, or a direct invocation) it is a no-op so behavior elsewhere is
# unchanged. Always exits 0 (afterFileEdit has no blocking semantics). Logs to
# stderr, which surfaces in Cursor's Hooks output channel.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
EVENT=$(get_hook_event "$INPUT")

# Scope strictly to the Cursor afterFileEdit event.
[ "$EVENT" = "afterFileEdit" ] || exit 0

FILE_PATH=$(get_edit_file_path "$INPUT")
[ -z "$FILE_PATH" ] && exit 0

# Ignore the runtime's internal scratch files.
is_agent_tools_path "$FILE_PATH" && exit 0

NEW_CONTENT=$(get_edit_new_content "$INPUT")
[ -z "$NEW_CONTENT" ] && exit 0

# No secret in the edit's new content → nothing to contain.
FOUND=$(scan_for_secret "$NEW_CONTENT") || exit 0

[ -f "$FILE_PATH" ] || exit 0

# Confirm the secret is actually on disk (the edit may have been superseded).
ON_DISK=$(cat "$FILE_PATH")
scan_for_secret "$ON_DISK" >/dev/null || exit 0

# ── Back up before any mutation (recovery guarantee) ────────────────
BACKUP="${FILE_PATH}.secret-revert.$(date +%Y%m%d%H%M%S).bak"
if ! cp -p "$FILE_PATH" "$BACKUP" 2>/dev/null; then
  log "secrets-scan-revert: could NOT back up $FILE_PATH ($FOUND) — refusing to mutate. Remove the secret manually."
  exit 0
fi

# ── Surgical redaction: drop only lines that contain a secret ───────
# Read line by line, skipping any line that matches a secret pattern. This
# neutralizes the secret without disturbing unrelated content. Preserves the
# original trailing-newline state byte-for-byte.
TMP=$(mktemp 2>/dev/null) || { log "secrets-scan-revert: mktemp failed for $FILE_PATH ($FOUND)."; exit 0; }
trap 'rm -f "$TMP"' EXIT

removed=0
while IFS= read -r line || [ -n "$line" ]; do
  if scan_for_secret "$line" >/dev/null; then
    removed=$((removed + 1))
    continue
  fi
  printf '%s\n' "$line" >> "$TMP"
done < "$FILE_PATH"

# Match the original trailing-newline state: if the file did NOT end in a
# newline, strip the one our loop appended to the final retained line.
if [ -s "$FILE_PATH" ] && [ "$(tail -c1 "$FILE_PATH" | wc -l | tr -d ' ')" -eq 0 ]; then
  # Original had no trailing newline — remove the trailing newline we added.
  if [ -s "$TMP" ]; then
    printf '%s' "$(cat "$TMP")" > "$TMP.adj" && mv "$TMP.adj" "$TMP"
  fi
fi

# Only commit the redaction if it actually produced a clean file.
REDACTED=$(cat "$TMP")
if [ "$removed" -gt 0 ] && ! scan_for_secret "$REDACTED" >/dev/null; then
  if cp "$TMP" "$FILE_PATH" 2>/dev/null; then
    log "secrets-scan-revert: redacted $removed secret-bearing line(s) from $FILE_PATH ($FOUND). Backup: $BACKUP. Secret was NOT committed; use environment variables."
    exit 0
  fi
fi

# Could not safely contain (secret spans non-line-removable content, or write
# failed). Leave the file as the user left it; the backup exists and the
# commit-time gate will still block. Log loudly.
log "secrets-scan-revert: could NOT surgically contain secret in $FILE_PATH ($FOUND). File left unchanged; backup at $BACKUP. The commit-time secrets-scan will block this until removed."
exit 0
