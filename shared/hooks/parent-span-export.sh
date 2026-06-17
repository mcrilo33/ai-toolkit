#!/usr/bin/env bash
# parent-span-export.sh — PreToolUse(Bash) hook: record the Bash call's tool_use_id
# as the current causal parent (Issue #66, telemetry Phase 2) so the scripts that
# command runs — and the native git-hooks those scripts trigger — emit their
# telemetry spans with it as `parent_id`. The dashboard's causal model
# (docs/dashboard-spoke-trace-scope.md) then nests them under the Bash tool node
# the parser derives from the same tool_use_id (Phase 1 owns that mapping).
#
# WHY A FILE, NOT A COMMAND REWRITE — a Claude Code PreToolUse hook cannot set an
# env var on the about-to-run command. The only per-call lever is rewriting the
# command text via `hookSpecificOutput.updatedInput`, but prepending a leading
# `AI_TOOLKIT_PARENT_SPAN=<id> ` assignment would BREAK the exact-match Bash
# permission allowlist: env-assignment prefixes are NOT stripped before matching
# (GitHub anthropics/claude-code#15292, closed not planned), so every allowlisted
# spoke command (`Bash(bash .ai-toolkit/scripts/spoke-push.sh:*)`, the push/ready
# rules, …) would re-prompt. So instead of touching the command, this hook writes
# the tool_use_id to `<root>/.ai-toolkit/parent-span`. telemetry.sh reads that file
# when resolving parent_id (after $AI_TOOLKIT_PARENT_SPAN, before the spoke root),
# and a native git-hook running in the same worktree reads the very same file.
# Shell→shell boundaries (a script invoking a child script) still propagate via the
# $AI_TOOLKIT_PARENT_SPAN env a parent exports for its child — which outranks the
# file — so script→script causality is exact.
#
# INVISIBLE + NON-BLOCKING — gated on AI_TOOLKIT_TELEMETRY=1; every failure path
# exits 0, nothing is written to stdout (no permission decision, so it composes
# with the deny/allow scope-guard hooks), and the file write is failure-swallowed.
# It does NOT source lib/utils.sh: that arms a per-hook span at exit, and this hook
# fires on EVERY Bash call — a span per command would be pure noise. It sources
# only the (side-effect-free) emit lib for its project-root resolver, so the file
# it writes is exactly the file telemetry.sh later reads.

# Telemetry opt-in gate. When off, touch nothing.
[ "${AI_TOOLKIT_TELEMETRY:-}" = "1" ] || exit 0

# Without jq we cannot safely read the payload; degrade to a no-op.
command -v jq >/dev/null 2>&1 || exit 0

# Read the payload (capped at 1MB, matching read_stdin in lib/utils.sh). Exposed as
# $INPUT so the sourced resolver can read workspace_roots from it.
INPUT="$(head -c 1048576)"
[ -n "$INPUT" ] || exit 0

id=$(printf '%s' "$INPUT" | jq -r '.tool_use_id // empty' 2>/dev/null) || exit 0
[ -n "$id" ] || exit 0

# Only a clean, opaque id is recorded (defense-in-depth; Claude Code mints
# `toolu_…`). Anything else is dropped rather than written verbatim.
case "$id" in
  *[!A-Za-z0-9_-]*) exit 0 ;;
esac

# Resolve the worktree root exactly as telemetry.sh does, so the file we write is
# the file it reads. The emit lib only DEFINES functions (the hook auto-span is
# armed by utils.sh, which we deliberately do not source), so this stays silent.
_PSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/telemetry.sh
. "$_PSE_DIR/lib/telemetry.sh" 2>/dev/null || exit 0
root="$(_telemetry_project_root 2>/dev/null)" || exit 0
[ -n "$root" ] || exit 0

{
  mkdir -p "$root/.ai-toolkit" \
    && printf '%s\n' "$id" > "$root/.ai-toolkit/parent-span"
} >/dev/null 2>&1 || true
exit 0
