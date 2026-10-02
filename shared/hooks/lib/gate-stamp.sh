#!/usr/bin/env bash
# gate-stamp.sh — green-tree stamp storage (issue #122).
#
# Since #378 the pre-push gate (test-select.sh) no longer runs or stamps full suites:
# CI is the gate. What remains here is the storage half gate-sweep.sh reads and
# writes. Nothing mints stamps or spawns that sweep any more, so this lib and gate-sweep.sh
# are inert (outside #378's Scope; a follow-up deletes them):
#   • KEY: `git rev-parse HEAD^{tree}` — any tracked change (tests included)
#     yields a new tree, so invalidation is structural, never time-based.
#   • PLACE: <git-common-dir>/.gate-stamps/<tree> — shared by the hub and every
#     spoke worktree, never in-tree, never pushed.
#   • CONTENT: tier=<tier>, env=<runner fingerprint>.
# Only scripted control-plane code mints stamps.

# Print the absolute stamp directory <git-common-dir>/.gate-stamps. The main
# checkout reports a relative --git-common-dir (".git"); absolutize it the way
# hub-ready-watch.sh does so worktree and hub agree on one directory.
gate_stamp_dir() {
  local common
  common="$(git rev-parse --git-common-dir 2>/dev/null)" || return 1
  [ -n "$common" ] || return 1
  case "$common" in
    /*) ;;
    *)  common="$PWD/$common" ;;
  esac
  printf '%s/.gate-stamps' "$common"
}

# Print the stamp key (HEAD^{tree}) — ONLY for a clean working tree. The suite
# runs against the working tree while the key names HEAD's tree; with tracked
# modifications or untracked files present the proof would not match the key,
# so a dirty tree yields no key at all: neither mint nor consume.
gate_stamp_tree() {
  [ -z "$(git status --porcelain 2>/dev/null)" ] || return 1
  git rev-parse 'HEAD^{tree}' 2>/dev/null
}

# gate_stamp_mint <tree> <tier> <env> — record a passing run, then GC stamps older
# than ~14 days. Temp-file + mv keeps concurrent writers atomic; two writers racing
# on the same tree write identical content, so last-write-wins is harmless. The GC
# also sweeps any orphaned temp files.
gate_stamp_mint() {
  local tree="$1" tier="$2" env_fp="$3" dir tmp
  dir="$(gate_stamp_dir)" || return 1
  mkdir -p "$dir"
  tmp="$(mktemp "$dir/.mint.XXXXXX")" || return 1
  printf 'tier=%s\nenv=%s\n' "$tier" "$env_fp" > "$tmp"
  mv -f "$tmp" "$dir/$tree"
  find "$dir" -type f -mtime +14 -delete 2>/dev/null || true
}
