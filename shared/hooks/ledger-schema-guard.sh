#!/usr/bin/env bash
# ledger-schema-guard.sh — PreToolUse (Claude): enforce the todo-ledger entry
# schema so scripted cycle steps and ad-hoc todos stay aggregable (Issue #235).
#
# Every ledger entry subject must read:
#
#   <subtask-id> · <STEP|type> — <label>
#
# where <subtask-id> is a `#<issue>.<slug>` handle, the keyword is one of the five
# solo-cycle steps (ANCHOR/RED/GREEN/REVIEW/PUSH) OR — for an ad-hoc entry — a type
# from investigate|fix|test|docs|chore|recover, and <label> is free text. This kills
# the two shapes #235 was filed over: a merged `step:REVIEW + sync + PUSH` entry and
# free-form todos that no aggregation can bucket. A non-conforming TaskCreate or
# subject-changing TaskUpdate is DENIED with the expected format (same UX as
# commit-gauntlet); a status-only TaskUpdate (no subject) passes untouched.
#
# SCOPE — a solo-cycle is a SPOKE concept, so this is gated on WT_SPOKE (the
# mechanical spoke-role signal worktree-new.sh sets); the hub and /quick lanes do
# not set it, so their ledgers are never blocked. A missing jq degrades to ALLOW —
# a broken environment must never wedge every TaskCreate/TaskUpdate.
#
# The ` · ` (U+00B7) and ` — ` (U+2014) separators are matched as LITERAL BYTES
# under LC_ALL=C (never a multibyte character class), so the match is identical
# regardless of the host locale — the one locale trap this repo keeps hitting.

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/utils.sh
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
[ -n "$INPUT" ] || exit 0

# Spoke-only + jq-required (see header). Both degrade to ALLOW, never block.
[ -n "${WT_SPOKE:-}" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

TOOL_NAME=$(get_tool_name "$INPUT")
case "$TOOL_NAME" in
  TaskCreate | TaskUpdate) ;;
  *) exit 0 ;;
esac

TOOL_INPUT=$(get_tool_input "$INPUT")
SUBJECT=$(printf '%s' "$TOOL_INPUT" | jq -r '.subject // empty' 2>/dev/null || true)
# A status-only TaskUpdate carries no subject — nothing to validate.
[ -n "$SUBJECT" ] || exit 0

# The closed keyword sets. STEP is a solo-cycle phase; TYPE tags an ad-hoc entry.
STEPS='ANCHOR|RED|GREEN|REVIEW|PUSH'
TYPES='investigate|fix|test|docs|chore|recover'
# `#<id> · <keyword> — <label>`, keyword from the closed sets, label non-empty. The
# `·`/`—` are the literal UTF-8 bytes of the separators; under LC_ALL=C grep treats
# them as a fixed byte sequence, so the match never depends on locale collation.
PATTERN="^#[^[:space:]]+ · (${STEPS}|${TYPES}) — .+$"
if printf '%s' "$SUBJECT" | LC_ALL=C grep -Eq "$PATTERN"; then
  exit 0
fi

deny "Ledger entry does not match the required schema (#235).
  Got:      ${SUBJECT}
  Expected: <subtask-id> · <STEP|type> — <label>
            e.g.  #<issue>.<slug> · RED — pin the failing behavior
  STEP is one of: ANCHOR RED GREEN REVIEW PUSH
  An ad-hoc entry instead carries a type: investigate fix test docs chore recover
  Use the ' · ' (middle dot) and ' — ' (em dash) separators verbatim. A ledger
  skeleton is pre-seeded at .ai-toolkit/ledger-skeleton.md — copy its rows."
