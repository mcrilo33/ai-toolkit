#!/usr/bin/env bash
# parent-span-export.sh — PreToolUse(Bash) hook: propagate one causal correlation
# id across every exec boundary (Issue #66, telemetry Phase 2).
#
# It stamps the Bash call's `tool_use_id` onto the command's environment as
# AI_TOOLKIT_PARENT_SPAN, so every script the command runs — and every native
# git-hook those scripts trigger (they inherit the env) — emits its telemetry span
# with that id as `parent_id`. The dashboard's causal model
# (docs/dashboard-spoke-trace-scope.md) then nests those spans under the Bash tool
# node the parser derives from the same tool_use_id (Phase 1 owns that mapping).
#
# MECHANISM — Claude Code PreToolUse hooks cannot set an env var for the tool
# directly; the only per-call lever is rewriting the command via
# `hookSpecificOutput.updatedInput`. We prepend a single **leading VAR=value
# assignment** (`AI_TOOLKIT_PARENT_SPAN=<id> <command>`), NOT an `export …;`
# statement. Two reasons:
#   1. A leading bare assignment exports the var into the command's process (and
#      thus its children) for that one invocation — exactly the inheritance we want.
#   2. Claude Code strips a leading `VAR=value` assignment before matching Bash
#      permission rules, so exact-match allow rules such as
#      `Bash(bash .ai-toolkit/scripts/spoke-push.sh:*)` keep matching and never
#      re-prompt. An `export X=…; cmd` prefix would instead split into a SECOND
#      subcommand (`;` is a recognized separator), and the spoke allowlist — built
#      precisely to make those a single allowlistable command (#34/#37/#45) — would
#      break. The leading-assignment form is the only allowlist-safe one.
#
# SAFETY — this hook never blocks and never fails a command. Telemetry off, no jq,
# an unparseable payload, a missing/odd `tool_use_id`, or an already-stamped
# command all degrade to NO rewrite (the command runs verbatim). It emits no
# permission decision, so it composes with the deny/allow scope-guard hooks.
#
# This file is self-contained on purpose: it does NOT source lib/utils.sh. That
# lib auto-emits a kind=hook span at exit, and this hook fires on EVERY Bash call —
# emitting a span per command would be pure noise. Its only job is the rewrite.

# Telemetry opt-in gate. When off, touch nothing — a non-telemetry user's command
# strings and allowlist are never perturbed by this hook.
[ "${AI_TOOLKIT_TELEMETRY:-}" = "1" ] || exit 0

# Without jq we cannot safely read the payload or build the output; degrade to a
# no-op rather than risk a malformed rewrite.
command -v jq >/dev/null 2>&1 || exit 0

# Read the payload (capped at 1MB, matching read_stdin in lib/utils.sh).
INPUT="$(head -c 1048576)"
[ -n "$INPUT" ] || exit 0

id=$(printf '%s' "$INPUT" | jq -r '.tool_use_id // empty' 2>/dev/null) || exit 0
[ -n "$id" ] || exit 0

# Only a clean id (the shape Claude Code mints, e.g. `toolu_01AbC…`) becomes a bare
# shell assignment value. Anything else is dropped rather than risk a token that
# could break out of the leading assignment.
case "$id" in
  *[!A-Za-z0-9_-]*) exit 0 ;;
esac

cmd=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
[ -n "$cmd" ] || exit 0

# Idempotent: never double-stamp a command that already carries the assignment
# (e.g. a re-entrant rewrite, or a command a parent already prefixed).
case "$cmd" in
  AI_TOOLKIT_PARENT_SPAN=*) exit 0 ;;
esac

# Rewrite via updatedInput. Preserve every OTHER tool_input field
# (run_in_background, timeout, description, …) and only prepend the leading
# assignment to .command, so the rewrite never drops a flag the model set.
printf '%s' "$INPUT" | jq -c --arg pfx "AI_TOOLKIT_PARENT_SPAN=$id " '
  .tool_input as $ti
  | {
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        updatedInput: ($ti + { command: ($pfx + $ti.command) })
      }
    }' 2>/dev/null || exit 0
exit 0
