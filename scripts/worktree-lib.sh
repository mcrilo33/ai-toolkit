#!/usr/bin/env bash
#
# worktree-lib.sh — shared helpers for worktree-new.sh and worktree-done.sh.
# Source this file; do not execute it. Callers set WT_PROG to their program name
# so diagnostics are attributed correctly.
#
# The two scripts MUST agree on slugify rules and on how a user-supplied target
# resolves to a worktree, so that anything you can create you can also tear down.
# Keeping that logic here is what guarantees it.

# --- diagnostics --------------------------------------------------------------

wt_die()  { printf '%s: %s\n' "${WT_PROG:-worktree}" "$*" >&2; exit 1; }
wt_warn() { printf '%s: %s\n' "${WT_PROG:-worktree}" "$*" >&2; }

# --- telemetry (opt-in, optional) ---------------------------------------------
# Source the shared span emit layer if present, so the worktree scripts can emit
# lifecycle spans. It is self-contained and gated by AI_TOOLKIT_TELEMETRY=1, so
# sourcing it is a no-op when telemetry is off. Locate it relative to THIS lib:
# in the ai-toolkit checkout it lives under shared/hooks/lib/; in a synced target
# the sync co-locates it next to these scripts in .ai-toolkit/scripts/.
_WT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _c in "$_WT_LIB_DIR/telemetry.sh" "$_WT_LIB_DIR/../shared/hooks/lib/telemetry.sh"; do
  if [ -f "$_c" ]; then . "$_c"; break; fi
done
unset _c

# Emit one lifecycle span for a worktree action, attributing it to the SPOKE:
# run the emit with the worktree as CWD so the span resolves that worktree's
# spoke_run_id / branch / repo. No-op when the emit layer or telemetry is absent.
# Usage: wt_emit_lifecycle <name> <phase> <status> <start_ms> <worktree_dir>
wt_emit_lifecycle() {
  command -v telemetry_emit_span >/dev/null 2>&1 || return 0
  local name="$1" phase="$2" status="$3" start_ms="$4" wt="$5"
  [ -d "$wt" ] || return 0
  ( cd "$wt" && telemetry_emit_span --kind lifecycle --name "$name" \
      --phase "$phase" --status "$status" --start-ms "$start_ms" ) || true
  return 0
}

# Emit one kind=script run-node span for a worktree script, attributing it to the
# SPOKE the same way wt_emit_lifecycle does (worktree as CWD). This is the control
# script as a first-class trace node (Issue #54); it shares its name with the
# lifecycle marker so the parser can later link marker→script via `emits`. The
# `emits` link stays null on push. No-op when the emit layer or telemetry is absent.
# Usage: wt_emit_script <name> <status> <start_ms> <worktree_dir>
wt_emit_script() {
  command -v telemetry_emit_span >/dev/null 2>&1 || return 0
  local name="$1" status="$2" start_ms="$3" wt="$4"
  [ -d "$wt" ] || return 0
  ( cd "$wt" && telemetry_emit_span --kind script --name "$name" \
      --status "$status" --start-ms "$start_ms" ) || true
  return 0
}

# Epoch-ms clock for span start times; empty string when the emit layer is
# absent (callers pass it through to wt_emit_lifecycle, which then defaults).
wt_now_ms() {
  command -v _telemetry_now_ms >/dev/null 2>&1 && _telemetry_now_ms || true
}

# --- portable date/time -------------------------------------------------------
# BSD (macOS) and GNU date differ; try the BSD form first, fall back to GNU.
# Kept here so the unattended supervisor (hub-afk.sh) and any future caller share
# one copy of the date/time helpers.

# wt_date_ymd <epoch> -> YYYY-MM-DD (local time).
wt_date_ymd() {
  date -r "$1" +%Y-%m-%d 2>/dev/null || date -d "@$1" +%Y-%m-%d
}

# wt_epoch_at <yyyy-mm-dd> <hh:mm> -> epoch seconds (local time).
# Seconds are pinned to :00 explicitly: BSD `date -j -f` fills a missing %S field
# from the current wall clock, which would leak the invocation second into the
# result and could flip a one-minute cutoff/window decision.
wt_epoch_at() {
  date -j -f "%Y-%m-%d %H:%M:%S" "$1 $2:00" +%s 2>/dev/null || date -d "$1 $2" +%s
}

# --- paths --------------------------------------------------------------------

# Canonical absolute path (resolves symlinks, e.g. /tmp -> /private/tmp on macOS).
# Empty output if the path does not exist.
wt_realpath() { (cd "$1" 2>/dev/null && pwd -P) || true; }

# Absolute, canonical path of the MAIN worktree — the first entry of
# `git worktree list`. Correct even when called from inside a linked worktree,
# which is why both scripts use this instead of `git rev-parse --show-toplevel`.
wt_main_root() {
  local p
  p="$(git worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2; exit}')"
  [ -n "$p" ] || return 1
  wt_realpath "$p"
}

# --- slug ---------------------------------------------------------------------

# Lowercase, collapse non-alphanumeric runs to '-', strip edges, keep <=4 segments.
# Both creation and teardown run identical input through this, so a raw arg
# normalizes the same way on both sides.
wt_slugify() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' \
    | cut -d- -f1-4
}

# --- tmux session name --------------------------------------------------------

# Derive a stable, tmux-safe session name for a repo root: parent-dir prefix +
# basename ('<parent>-<base>'), so two repos sharing a basename under different
# parents get distinct sessions and 'tmux ls' reads as a per-project portfolio.
# tmux forbids '.' and ':' in session names → map them to '-'. The caller passes
# the canonical main-worktree root, so the result is deterministic per repo.
wt_tmux_session() {
  local root="$1" parent base
  parent="$(basename "$(dirname "$root")")"
  base="$(basename "$root")"
  printf '%s-%s' "$parent" "$base" | tr '.:' '-'
}

# --- worktree enumeration / resolution ---------------------------------------

# Emit "path<TAB>branch" (branch without refs/heads/) for every worktree EXCEPT
# the main one. Detached worktrees emit an empty branch field. Handles the
# porcelain stream's lack of a trailing blank line by flushing at EOF.
# Args: $1 = canonical main root.
wt_task_worktrees() {
  local main="$1" wt="" br=""
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) wt="${line#worktree }"; br="" ;;
      "branch "*)   br="${line#branch }"; br="${br#refs/heads/}" ;;
      "")
        if [ -n "$wt" ] && [ "$(wt_realpath "$wt")" != "$main" ]; then
          printf '%s\t%s\n' "$wt" "$br"
        fi
        wt=""; br=""
        ;;
    esac
  done < <(git worktree list --porcelain 2>/dev/null)
  if [ -n "$wt" ] && [ "$(wt_realpath "$wt")" != "$main" ]; then
    printf '%s\t%s\n' "$wt" "$br"
  fi
}

# Pretty-print the task worktrees to stderr (path + branch), for error recovery.
# Args: $1 = canonical main root.
wt_print_worktrees() {
  local main="$1" any="" wt br
  while IFS=$'\t' read -r wt br; do
    [ -n "$wt" ] || continue
    any=1
    printf '    %-50s %s\n' "$wt" "${br:-(detached)}" >&2
  done < <(wt_task_worktrees "$main")
  [ -n "$any" ] || printf '    (none)\n' >&2
}

# Resolve a user-supplied target to exactly one task-worktree path.
# Matches a target against each worktree by, in order of intent:
#   - canonical path equality (target is/locates a worktree dir)
#   - directory basename, or its tag (basename with the "<repo>-" prefix stripped)
#   - the slugified target vs that tag (so raw "Refactor_Sync" finds "refactor-sync")
#   - the full branch name, or the branch's trailing slug
#   - the leading issue number of the branch slug (so "42" finds feature/42-foo)
# Prints the single match on stdout and returns 0. On zero or multiple matches it
# returns 1 — the caller is expected to list candidates and exit.
# Args: $1 = target, $2 = canonical main root.
wt_resolve() {
  local target="$1" main="$2"
  local tslug repo trp wt br base tag bslug bnum
  tslug="$(wt_slugify "$target")"
  repo="$(basename "$main")"
  trp="$(wt_realpath "$target")"

  local matches=() seen=""
  while IFS=$'\t' read -r wt br; do
    [ -n "$wt" ] || continue
    base="$(basename "$wt")"
    tag="${base#"${repo}-"}"
    bslug="${br##*/}"
    bnum="${bslug%%-*}"
    if { [ -n "$trp" ] && [ "$trp" = "$(wt_realpath "$wt")" ]; } \
       || [ "$target" = "$base" ] \
       || [ "$target" = "$tag" ] || [ "$tslug" = "$tag" ] \
       || { [ -n "$br" ] && [ "$target" = "$br" ]; } \
       || { [ -n "$bslug" ] && { [ "$target" = "$bslug" ] || [ "$tslug" = "$bslug" ]; }; } \
       || { [ -n "$bnum" ] && [ "$bnum" != "$bslug" ] && [ "$target" = "$bnum" ]; }; then
      case "$seen" in
        *"|$wt|"*) ;;            # already collected
        *) matches+=("$wt"); seen="${seen}|$wt|" ;;
      esac
    fi
  done < <(wt_task_worktrees "$main")

  if [ "${#matches[@]}" -eq 1 ]; then
    printf '%s\n' "${matches[0]}"
    return 0
  fi
  return 1
}

# --- native-OTel message-bridge preflight (auto-populate) --------------------
# A spoke that opted into native OTel (AI_TOOLKIT_OTEL=1) needs the Langfuse
# message bridge (scripts/telemetry/langfuse_message_bridge.py, port :4319) up, or
# the audit events (#93) and LLM request/response I/O the otelcol forks to it never
# reach Langfuse. These helpers bring it up idempotently at spawn so the operator
# runs no manual step; they are best-effort and never fail the spawn.

# True when something is LISTENing on the given localhost TCP port. Split out so
# the preflight decision is unit-testable by overriding it (no live socket). Uses
# lsof when present, else nc; when neither exists, reports "down" so the caller
# attempts a start (a duplicate would simply fail to bind — never two servers).
wt_port_listening() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  elif command -v nc >/dev/null 2>&1; then
    nc -z localhost "$port" >/dev/null 2>&1
  else
    return 1
  fi
}

# Start the message bridge in the background, detached, logging to a temp file.
# Split from the preflight so the decision logic can be tested without spawning a
# real server. PYTHONPATH=scripts lets the script import its sibling telemetry
# package; LANGFUSE_HOST defaults to the local Langfuse. The child reads its
# REQUIRED config from the environment — LANGFUSE_BASIC_AUTH (KeyErrors without it)
# and BRIDGE_PORT — so re-export them inside a subshell: a non-exported operator
# value still reaches the child (it passed the preflight as a shell-internal read),
# and the credential never lands on the python argv. Args: $1 = repo root.
wt_bridge_launch() {
  local repo_root="$1" log
  log="$(mktemp -t lf-bridge.XXXXXX 2>/dev/null)" || log="/tmp/lf-bridge.log"
  (
    export PYTHONPATH="$repo_root/scripts"
    export LANGFUSE_HOST="${LANGFUSE_HOST:-http://localhost:3000}"
    export LANGFUSE_BASIC_AUTH BRIDGE_PORT
    nohup python3 "$repo_root/scripts/telemetry/langfuse_message_bridge.py" \
      >"$log" 2>&1 &
  )
  echo "→ started Langfuse message bridge on :${BRIDGE_PORT:-4319} (log: $log)"
}

# Idempotently ensure the message bridge is up for an opted-in spoke. A no-op
# unless AI_TOOLKIT_OTEL=1. Never starts a second bridge (skips when :4319 already
# listens). When LANGFUSE_BASIC_AUTH is unset the bridge can't authenticate to
# Langfuse, so warn (audit events + LLM I/O won't land) but DO NOT fail the spawn.
# Args: $1 = repo root.
wt_otel_bridge_preflight() {
  local repo_root="$1" port="${BRIDGE_PORT:-4319}"
  [ "${AI_TOOLKIT_OTEL:-}" = "1" ] || return 0
  wt_port_listening "$port" && return 0
  if [ -z "${LANGFUSE_BASIC_AUTH:-}" ]; then
    wt_warn "OTel bridge down on :$port and LANGFUSE_BASIC_AUTH unset — audit events (#93) + LLM I/O won't reach Langfuse; spoke still launches"
    return 0
  fi
  wt_bridge_launch "$repo_root"
}
