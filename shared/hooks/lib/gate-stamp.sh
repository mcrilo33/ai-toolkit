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
#   • CONTENT: tier=<full|selected|testmon>, env=<runner fingerprint>, and for
#     the selected tier (issue #123) set=<comma-joined sorted test files that
#     ran> plus testmon=1 when the green run also proved testmon (mixed diff).
# Only the gate script mints stamps (scripted control plane). Coverage rules
# (issue #123 replaced the pure rank order — a selection proves ONLY the set
# it names):
#   • a full stamp covers every demand;
#   • a testmon demand is covered by a full or testmon stamp — never by a
#     selection, which ran only its named files, not testmon's impact set;
#   • a selected demand (set S, possibly mixed) is covered by a full stamp,
#     or by a selected stamp whose recorded set ⊇ S (and which also carries
#     testmon=1 when the demand is mixed). A legacy set-less selected stamp
#     covers nothing.
# The env fingerprint must always match exactly; a weaker stamp does not
# skip, and the passing run that follows upgrades it (mint overwrites
# unconditionally).

# _gate_stamp_csv_subset <want-csv> <have-csv> — every comma-separated item of
# `want` appears in `have`. Items are repo-relative test paths, which contain
# neither commas nor spaces (the same assumption their argv hand-off to pytest
# already makes).
_gate_stamp_csv_subset() {
  local want="$1" have="$2" item
  local IFS=','
  for item in $want; do
    case ",$have," in
      *",$item,"*) ;;
      *) return 1 ;;
    esac
  done
  return 0
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

# gate_stamp_mint <tree> <tier> <env> [<set-csv> [<testmon-flag>]] — record a
# passing run, then GC stamps older than ~14 days. A selected mint passes the
# set that ran (and testmon-flag=1 when the mixed run proved testmon too).
# Temp-file + mv keeps concurrent gates atomic; two gates racing on the same
# tree write identical content, so last-write-wins is harmless. The GC also
# sweeps any orphaned temp files.
gate_stamp_mint() {
  local tree="$1" tier="$2" env_fp="$3" set_csv="${4:-}" testmon_flag="${5:-}" dir tmp
  dir="$(gate_stamp_dir)" || return 1
  mkdir -p "$dir"
  tmp="$(mktemp "$dir/.mint.XXXXXX")" || return 1
  printf 'tier=%s\nenv=%s\n' "$tier" "$env_fp" > "$tmp"
  if [ -n "$set_csv" ]; then
    printf 'set=%s\n' "$set_csv" >> "$tmp"
  fi
  if [ "$testmon_flag" = "1" ]; then
    printf 'testmon=1\n' >> "$tmp"
  fi
  mv -f "$tmp" "$dir/$tree"
  find "$dir" -type f -mtime +14 -delete 2>/dev/null || true
}

# gate_stamp_has <tree> — a stamp file exists for the tree (any tier/env).
# Lets the gate defer its runner `--version` fingerprint probe until a stamp
# could actually be consumed: probing invokes the runner, which must stay off
# the common no-stamp path (it runs before the repo-integrity tripwire).
gate_stamp_has() {
  local dir
  dir="$(gate_stamp_dir)" || return 1
  [ -f "$dir/$1" ]
}

# gate_stamp_check <tree> <demanded-tier> <env> [<demand-set-csv> [<mixed>]] —
# succeed iff a stamp covers the demand: same tree, exact env-fingerprint
# match, and the coverage rules from the header (a selection proves only the
# set it names). The two optional args matter only for a selected demand.
gate_stamp_check() {
  local tree="$1" demanded="$2" env_fp="$3" dset="${4:-}" dmixed="${5:-}"
  local dir stamp stamped_tier stamped_env stamped_set stamped_testmon
  dir="$(gate_stamp_dir)" || return 1
  stamp="$dir/$tree"
  [ -f "$stamp" ] || return 1
  stamped_tier="$(sed -n 's/^tier=//p' "$stamp" | head -1)"
  stamped_env="$(sed -n 's/^env=//p' "$stamp" | head -1)"
  [ "$stamped_env" = "$env_fp" ] || return 1
  case "$demanded" in
    full)
      [ "$stamped_tier" = "full" ]
      ;;
    testmon)
      [ "$stamped_tier" = "full" ] || [ "$stamped_tier" = "testmon" ]
      ;;
    selected)
      if [ "$stamped_tier" = "full" ]; then return 0; fi
      [ "$stamped_tier" = "selected" ] || return 1
      [ -n "$dset" ] || return 1   # a set-less demand names nothing provable
      stamped_set="$(sed -n 's/^set=//p' "$stamp" | head -1)"
      [ -n "$stamped_set" ] || return 1   # legacy bare stamp covers nothing
      _gate_stamp_csv_subset "$dset" "$stamped_set" || return 1
      if [ "$dmixed" = "1" ]; then
        stamped_testmon="$(sed -n 's/^testmon=//p' "$stamp" | head -1)"
        [ "$stamped_testmon" = "1" ] || return 1
      fi
      return 0
      ;;
    *)
      return 1   # an unknown demand is never satisfied
      ;;
  esac
}
