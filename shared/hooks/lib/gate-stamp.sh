#!/usr/bin/env bash
# gate-stamp.sh — green-tree stamp storage for the pre-push test gate (issue #122).
#
# The gate (test-select.sh) re-ran suites for trees it had already proven green:
# every FF-land re-ran a ~6-min suite on a tree a fast-forward could not have
# changed. These helpers make that proof durable and content-addressed:
#   • KEY: `git rev-parse HEAD^{tree}` — any tracked change (tests included)
#     yields a new tree, so invalidation is structural, never time-based.
#   • PLACE: <git-common-dir>/.gate-stamps/<tree> — shared by the hub and every
#     spoke worktree, never in-tree, never pushed (same placement rationale as
#     hub-ready-watch's last-seen set).
#   • CONTENT: tier=<full|selected|testmon> and env=<runner fingerprint>.
# Only the gate script mints stamps (scripted control plane). A consumer skips
# only when the stamped tier is at least as strong as the demanded one AND the
# env fingerprint matches exactly; a weaker stamp does not skip, and the
# passing run that follows upgrades it (mint overwrites unconditionally).

# Rank a tier on the strength order full(3) > selected(2) > testmon(1).
# Unknown tiers rank 0: an unknown stamp never covers, an unknown demand is
# never satisfied.
_gate_stamp_tier_rank() {
  case "$1" in
    full)     echo 3 ;;
    selected) echo 2 ;;
    testmon)  echo 1 ;;
    *)        echo 0 ;;
  esac
}

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

# gate_stamp_mint <tree> <tier> <env> — record a passing run, then GC stamps
# older than ~14 days. Temp-file + mv keeps concurrent gates atomic; two gates
# racing on the same tree write identical content, so last-write-wins is
# harmless. The GC also sweeps any orphaned temp files.
gate_stamp_mint() {
  local tree="$1" tier="$2" env_fp="$3" dir tmp
  dir="$(gate_stamp_dir)" || return 1
  mkdir -p "$dir"
  tmp="$(mktemp "$dir/.mint.XXXXXX")" || return 1
  printf 'tier=%s\nenv=%s\n' "$tier" "$env_fp" > "$tmp"
  mv -f "$tmp" "$dir/$tree"
  find "$dir" -type f -mtime +14 -delete 2>/dev/null || true
}

# gate_stamp_check <tree> <demanded-tier> <env> — succeed iff a stamp covers
# the demand: same tree, exact env-fingerprint match, stamped tier at least as
# strong as the demanded one.
gate_stamp_check() {
  local tree="$1" demanded="$2" env_fp="$3" dir stamp stamped_tier stamped_env
  dir="$(gate_stamp_dir)" || return 1
  stamp="$dir/$tree"
  [ -f "$stamp" ] || return 1
  stamped_tier="$(sed -n 's/^tier=//p' "$stamp" | head -1)"
  stamped_env="$(sed -n 's/^env=//p' "$stamp" | head -1)"
  [ "$stamped_env" = "$env_fp" ] || return 1
  local have want
  have="$(_gate_stamp_tier_rank "$stamped_tier")"
  want="$(_gate_stamp_tier_rank "$demanded")"
  [ "$want" -gt 0 ] && [ "$have" -ge "$want" ]
}
