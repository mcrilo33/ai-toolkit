#!/usr/bin/env bash
# gate-broker-answerer.sh -- split out of gate-broker.sh (issue #275).
#
# A pure function-definition module of the gate-broker core. Sourced by the entry lib
# gate-broker.sh AFTER worktree-lib/hub-inject/log/afk_now and BEFORE any function is
# called, so every cross-module helper resolves at call time. Not run on its own.
set -uo pipefail

# --- the answerer (the one reasoning step) ------------------------------------

# --- read-only worktree reasoner (issue #155, subtask B) ----------------------
# The gate reasoner gets READ-ONLY access to the spoke's LIVE worktree (cwd) so it can
# verify a decision against real state — uncommitted/staged included — before auto-
# answering: evidence, not a pattern-guess. Two enforcement layers:
#   1. PREVENTION — run with a read-only tool allowlist (the code-review/Explore
#      posture: Read/Grep/Glob + a narrow read-only git helper; never Edit/Write).
#   2. DETECTION — a content fingerprint of the worktree taken before and after the
#      reason step; ANY change is a read-only BREACH, so the answer is voided and the
#      gate routes to a human. Detection is the HARD guarantee: it does not depend on
#      the LLM honoring the allowlist.

# reasoner_allowed_tools -> the read-only allowlist passed to the headless reasoner
# (comma-joined for `claude --allowedTools`). Read/Grep/Glob plus narrow read-only git
# verbs via scoped Bash patterns — enough to inspect the tree and run status/diff to
# verify a plan, nothing that can mutate it. AFK_REASONER_TOOLS overrides.
# UPGRADE: confirm the exact `claude --allowedTools` list/pattern syntax against the
# installed CLI version if the reasoner ever reports a read tool it should have.
reasoner_allowed_tools() {
  printf '%s\n' "${AFK_REASONER_TOOLS:-Read,Grep,Glob,Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git rev-parse:*)}"
}

# _reasoner_bash_readonly <inner> -> rc 0 when a scoped Bash allow pattern's inner
# command is a read-only git verb (git status/diff/log/show/rev-parse/branch/ls-files/
# cat-file), rc 1 otherwise. Keeps a `Bash(...)` allow from smuggling a mutating verb
# (git push/commit/reset, rm, chmod, …) past assert_readonly_tools.
_reasoner_bash_readonly() {
  case "$1" in
    'git status'* | 'git diff'* | 'git log'* | 'git show'* | 'git rev-parse'* \
      | 'git branch'* | 'git ls-files'* | 'git cat-file'*) return 0 ;;
    *) return 1 ;;
  esac
}

# assert_readonly_tools <comma-list> -> rc 0 when every tool is read-only, rc 1 when any
# is a mutating tool (Edit/Write/MultiEdit/NotebookEdit), a bare unrestricted Bash, or a
# scoped Bash(...) whose inner verb is NOT a read-only git verb. Anything unrecognised is
# denied (default-deny). Parses by hand (no word-splitting) so a glob in a Bash(...)
# pattern never expands.
assert_readonly_tools() {
  local rest="$1" tok inner
  while [ -n "$rest" ]; do
    tok="${rest%%,*}"
    if [ "$tok" = "$rest" ]; then rest=""; else rest="${rest#*,}"; fi
    tok="${tok#"${tok%%[![:space:]]*}"}"; tok="${tok%"${tok##*[![:space:]]}"}"   # trim
    [ -n "$tok" ] || continue
    case "$tok" in
      Read | Grep | Glob | LS | WebFetch | WebSearch | TodoRead) ;;
      'Bash('*')')                                     # a scoped Bash verb: vet it
        inner="${tok#Bash(}"; inner="${inner%)}"
        _reasoner_bash_readonly "$inner" || return 1 ;;
      *) return 1 ;;                                    # mutating / bare Bash / unknown -> deny
    esac
  done
  return 0
}

# _broker_worktree_fingerprint <wt> -> a content hash of the LIVE worktree's TRACKED content
# PLUS its untracked-not-ignored files: each path + its CURRENT working-tree content. A
# tracked edit, a staged addition, a deletion, OR a brand-new untracked-not-ignored file all
# change it. IGNORED files stay excluded on purpose (issue #168): a parked spoke is not a
# frozen worktree — its own still-finishing push gate writes `.testmondata`, OTel dumps land
# under `.ai-toolkit/`, etc. Those runtime artifacts are git-ignored, so they must not be
# blamed on the read-only reasoner. `--others --exclude-standard` (issue #203) closes the
# creation gap #168 opened — a reasoner that CREATES a new untracked file used to be invisible
# here, mutating the tree unprevented AND undetected — while keeping the #168 ignored-artifact
# class safe (the exclude honors .gitignore, .git/info/exclude, AND the global excludesFile).
# `sort -zu` makes the combined listing order-stable. THIS WORKTREE'S HEAD is folded in too
# (issue #239): `git rev-parse HEAD` so a reasoner ref write that moves HEAD (`git commit` /
# `update-ref` of the checked-out branch) — which the index/working-tree content scan can never
# see — still changes the fingerprint, backstopping the snapshot isolation should it ever
# regress. Deliberately NOT `git for-each-ref`: on a linked worktree that lists the SHARED refs,
# so ordinary concurrent /afk-drain activity (a sibling spoke's push, a hub auto-land advancing
# main, a background fetch) would flip the fingerprint and terminally FALSE-void a correct
# answer — the concurrent-sibling false-BREACH class this repo already fights. HEAD reflects only
# THIS worktree's own branch tip, immune to sibling ref churn. UPGRADE: to also catch a ref
# write that does NOT move HEAD (a stray tag / non-checked-out branch), fingerprint the
# worktree's own per-worktree refs specifically — never the shared ref namespace.
# Empty (stable) for a non-git or missing path, so a non-worktree reasoner never trips a
# false breach.
_broker_worktree_fingerprint() {
  local wt="$1"
  [ -d "$wt" ] || return 0
  (
    cd "$wt" 2>/dev/null || exit 0
    git rev-parse --git-dir >/dev/null 2>&1 || exit 0
    {
      git ls-files -z --cached --others --exclude-standard 2>/dev/null | sort -zu |
        while IFS= read -r -d '' f; do
          printf '%s\0' "$f"
          if [ -f "$f" ]; then git hash-object "$f" 2>/dev/null || printf 'ERR'; else printf 'GONE'; fi
          printf '\0'
        done
      printf 'HEAD\0'; git rev-parse -q --verify HEAD 2>/dev/null || printf 'NONE'; printf '\0'
    } |
      shasum -a 256 2>/dev/null | awk '{print $1}'
  )
}

# _broker_worktree_unchanged <wt> <before_fingerprint> -> rc 0 when the worktree is
# byte-for-byte what it was at <before_fingerprint>, rc 1 when the reasoner mutated it.
_broker_worktree_unchanged() {
  local wt="$1" before="$2" after
  after="$(_broker_worktree_fingerprint "$wt")"
  [ "$before" = "$after" ]
}

# _broker_is_git_worktree <wt> -> rc 0 when <wt> is a real git worktree (so a NON-empty
# fingerprint is expected). Used to fail SAFE: an empty fingerprint for a git worktree
# means the fingerprint tooling (shasum/git) is missing and the read-only guard can't
# verify — which must escalate, not silently pass.
_broker_is_git_worktree() {
  [ -d "$1" ] && git -C "$1" rev-parse --git-dir >/dev/null 2>&1
}

# _broker_snapshot_worktree <wt> <dest> -> populate <dest> with a throwaway COPY of <wt>'s
# content so the reasoner can run there (cwd=<dest>) instead of the spoke's LIVE tree — real
# write isolation (#237), the "verify agent worktree isolation" prior art: even a tool that
# ignores the read-only allowlist writes into the copy, never the spoke's tree. rc 0 on a
# populated copy, rc 1 when <wt> is not a git worktree (the caller then runs in-place and the
# fingerprint void still guards). The copy carries ONLY the tracked + untracked-not-ignored
# set (the SAME set _broker_worktree_fingerprint measures) plus the .git linkage, so a per-tick
# copy never recurses the ignored heavy trees (.venv, .testmondata*, .ai-toolkit/ OTel dumps).
# `cp -R` preserves the worktree's uncommitted + untracked state — fidelity `git worktree add`
# (committed-HEAD only) can't give — so the reasoner's read git verbs still reflect real state.
# LINKED-WORKTREE GITDIR ISOLATION (#239): a spoke is always a LINKED worktree, whose `.git` is
# a gitfile still pointing at the SHARED common gitdir. Copying that pointer verbatim (`cp -R`)
# leaves git WRITE-verbs in the copy (a tool that ignores the read-only allowlist) resolving to
# the real shared refs — `git commit`/`update-ref` in the copy moved the live HEAD/branch tip
# and the content-only fingerprint never saw it. So for the gitfile case we give the copy a
# PRIVATE, self-contained gitdir (_broker_private_gitdir): the object store is shared READ-ONLY
# via `objects/info/alternates` (no per-tick object copy), while refs/HEAD/index are copied so
# read verbs still reflect real state AND every write lands in the copy's own gitdir. The
# main-checkout `.git`-DIRECTORY fast path stays `cp -R` — a self-contained dir is already
# isolated wholesale.
_broker_snapshot_worktree() {
  local wt="$1" dest="$2" f
  _broker_is_git_worktree "$wt" || return 1
  # Provide the git linkage first so read-only git verbs resolve, then the exact fingerprint
  # set — never the ignored heavy trees. A `.git` DIRECTORY copies wholesale (already isolated);
  # a linked-worktree GITFILE gets a private gitdir so writes can't reach the shared common dir.
  if [ -d "$wt/.git" ]; then
    cp -R "$wt/.git" "$dest/.git" 2>/dev/null
  elif [ -f "$wt/.git" ]; then
    # Best-effort (like the old `cp -R … 2>/dev/null`): even a partial/failed private gitdir is
    # still a PRIVATE $dest/.git — never a gitfile pointing at the shared common dir — so keeping
    # the copy preserves write isolation. A hard `return 1` here would make run_answerer fall
    # back to running the reasoner in the LIVE tree, silently dropping the very isolation this
    # provides; the reasoner's git reads just degrade if the private gitdir is incomplete.
    _broker_private_gitdir "$wt" "$dest" || true
  fi
  (
    cd "$wt" 2>/dev/null || exit 0
    git ls-files -z --cached --others --exclude-standard 2>/dev/null |
      while IFS= read -r -d '' f; do
        [ -f "$f" ] || continue
        mkdir -p "$dest/$(dirname "$f")" 2>/dev/null || true
        cp -p "$f" "$dest/$f" 2>/dev/null || true
      done
  )
  return 0
}

# _broker_private_gitdir <wt> <dest> -> build a PRIVATE, self-contained gitdir at <dest>/.git
# for a LINKED worktree <wt> (whose own `.git` is a gitfile at the shared common gitdir), so a
# git write-verb in the copy writes ONLY here — never the shared refs (#239). Objects are shared
# READ-ONLY via alternates (cheap: no per-tick copy of the object store); the shared refs +
# packed-refs are copied so read verbs reflect real state and a ref write lands locally; HEAD +
# index come from the per-worktree gitdir so `git status`/`diff` reflect the spoke's real
# uncommitted state; the real common config is copied (with worktree-specific bits neutralized)
# so any `[extensions]` carry over. rc 1 on a failure the caller treats as best-effort — a
# partial $dest/.git is still private, so write isolation holds either way.
# UPGRADE: the ref copy assumes the `files` ref backend; a `reftable`-backend repo keeps refs in
# a `reftable/` dir, not `refs/` + `packed-refs`, and would need that copied instead.
_broker_private_gitdir() {
  local wt="$1" dest="$2" common gitdir
  common="$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null)" || return 1
  gitdir="$(git -C "$wt" rev-parse --absolute-git-dir 2>/dev/null)" || return 1
  [ -n "$common" ] && [ -n "$gitdir" ] || return 1
  case "$common" in /*) ;; *) common="$wt/$common" ;; esac   # resolve a relative common dir
  mkdir -p "$dest/.git/objects/info" "$dest/.git/refs" || return 1
  printf '%s\n' "$common/objects" > "$dest/.git/objects/info/alternates"
  cp -R "$common/refs/." "$dest/.git/refs/" 2>/dev/null || true
  [ -f "$common/packed-refs" ] && cp "$common/packed-refs" "$dest/.git/packed-refs" 2>/dev/null
  cp "$gitdir/HEAD" "$dest/.git/HEAD" 2>/dev/null || return 1
  [ -f "$gitdir/index" ] && cp "$gitdir/index" "$dest/.git/index" 2>/dev/null
  # Copy the REAL common config (not a hardcoded version-0 stub) so any `[extensions]` the shared
  # repo needs — objectformat=sha256, etc. — carry over and the shared objects still parse; then
  # neutralize the worktree-specific bits so the copy is a plain non-bare worktree rooted at $dest.
  if [ -f "$common/config" ]; then
    cp "$common/config" "$dest/.git/config" 2>/dev/null
  else
    printf '[core]\n\tbare = false\n' > "$dest/.git/config"
  fi
  git -C "$dest" config core.bare false 2>/dev/null || true
  git -C "$dest" config --unset core.worktree 2>/dev/null || true
  return 0
}

# read_decisions_digest <issue> -> a compact digest of THIS spoke's prior gate outcomes,
# seeded into the reasoner for cross-gate consistency (NOT the old transcript, which
# replayed the seed in #124). Reads the automatable-decisions log (subtask D's writer),
# filtered to this issue; empty when the log is absent. Shared line format (with #155
# subtask D): <ts>\t<issue>\t<gate_type>\t<signature>\t<decision>.
read_decisions_digest() {
  local issue="$1" log
  log="$(_afk_state_dir)/decisions.log"
  [ -f "$log" ] || return 0
  awk -F'\t' -v issue="$issue" '$2 == issue { printf "- %s: %s (%s)\n", $3, $5, $4 }' "$log" 2>/dev/null || true
}

# --- automatable-decisions log + codification (issue #155, subtask D) ----------
# Every automatable PERMISSION decision (the mechanical classify_permission verdict — the
# codifiable class; a reasoner ANSWER is free text and a plan gate is a judgment call, so
# neither is logged) is recorded with a normalized SIGNATURE so recurrences of the same
# command shape collide; an on-demand codification pass then proposes deterministic rules
# for signatures that recur unanimously — graduating common gates out of the LLM in BOTH
# modes (the "scripted control plane, not LLM" payoff, generalizing #149's git-reset
# self-stage rule into a learning pipeline). Proposal-only: a human reviews before any
# rule is appended to the classifier table.

# _normalize_command_shape <command> -> the command's verb skeleton: each ;/&&/||/|
# segment reduced to "<verb>-<subcommand>" (flags/args/paths dropped), joined by '+'. So
# `git reset -q; git add tests/x.py` and `git reset HEAD; git add a.py` both normalize to
# `git-reset+git-add`. Parses tokens by hand (no word-splitting) so a glob never expands.
_normalize_command_shape() {
  local cmd="$1" norm seg out="" verb rest sub part
  # Split on the same operators classify_permission does (&& and || before the single &
  # and | so they are not pre-split); the single & must split too, or `git status & rm`
  # would sign as only `git-status`.
  norm="${cmd//&&/$'\n'}"; norm="${norm//||/$'\n'}"
  norm="${norm//&/$'\n'}"; norm="${norm//|/$'\n'}"; norm="${norm//;/$'\n'}"
  while IFS= read -r seg; do
    seg="${seg#"${seg%%[![:space:]]*}"}"; seg="${seg%"${seg##*[![:space:]]}"}"
    [ -n "$seg" ] || continue
    verb="${seg%% *}"
    rest="${seg#"$verb"}"; rest="${rest#"${rest%%[![:space:]]*}"}"
    sub="${rest%% *}"
    case "$sub" in '' | -*) sub="" ;; esac        # a flag / nothing isn't a subcommand
    part="$verb"; [ -n "$sub" ] && part="$verb-$sub"
    out="${out:+$out+}$part"
  done <<<"$norm"
  printf '%s\n' "$out"
}

# _broker_decision_signature <gate_type> <shape> -> a stable signature for the decision.
# A permission gate's shape is its command (normalized to the verb skeleton); other gate
# types sign as the gate type itself (a plan gate is a judgment call, not codifiable).
_broker_decision_signature() {
  local gate_type="$1" shape="$2"
  case "$gate_type" in
    permission) _normalize_command_shape "$shape" ;;
    *) printf '%s\n' "$gate_type" ;;
  esac
}

# log_decision <issue> <gate_type> <shape> <decision> -> append one automatable-decisions
# record: <ts>\t<issue>\t<gate_type>\t<signature>\t<decision>. Exactly the format
# read_decisions_digest (subtask B) consumes. Best-effort; never aborts the caller.
log_decision() {
  local issue="$1" gate_type="$2" shape="$3" decision="$4" sig log
  sig="$(_broker_decision_signature "$gate_type" "$shape")"
  log="$(_afk_state_dir)/decisions.log"
  mkdir -p "$(dirname "$log")" 2>/dev/null || true
  printf '%s\t%s\t%s\t%s\t%s\n' "$(afk_now)" "$issue" "$gate_type" "$sig" "$decision" \
    >>"$log" 2>/dev/null || true
}

# codify_decisions [min_count] -> propose a deterministic rule for every signature that
# recurs at least <min_count> times (default 2) with a UNANIMOUS decision. Output is a
# PROPOSAL a human reviews before it is codified into classify_permission — never an
# auto-applied rule. A single-occurrence or a conflicting signature proposes nothing. The
# signature drops flags/args, so the proposal carries a "verify destructive flag variants"
# caveat: the human must confirm the shape is safe across the flags classify_permission
# distinguishes before codifying. Malformed lines (missing signature/decision) are skipped.
codify_decisions() {
  local min="${1:-2}" log
  log="$(_afk_state_dir)/decisions.log"
  [ -f "$log" ] || return 0
  awk -F'\t' -v min="$min" '
    $4 != "" && $5 != "" {
      sig=$4; dec=$5; count[sig]++
      if (!(sig in decision)) decision[sig]=dec
      else if (decision[sig]!=dec) conflict[sig]=1 }
    END {
      for (s in count)
        if (count[s] >= min && !(s in conflict))
          printf "RULE: %s -> %s (%d occurrences, unanimous; verify destructive flag variants)\n", s, decision[s], count[s]
    }' "$log" 2>/dev/null | sort || true
}

# --- #300 step 3b lifecycle transition-log lane events (shadow writers) --------
# The broker records the lane action it CAUSES — an answer computed / dropped / escalated, a
# #277 plan waive, a permission approve/deny — as a transition-log EVENT keyed by the park
# EPISODE, the same actor-records-the-cause principle #302 wired into the drain's recovery lane.
# Shadow-only: nothing reads the log for a decision in this step. Best-effort throughout: a write
# never fails the broker (guarded when the transition-log lib is unavailable) and adds NO pane
# read — the episode signature comes from the caller (the permission lane already holds it) or
# the PERSISTED park-sig record, never a fresh capture-pane on the shadow path (#269).

# _gb_episode_key <issue> [sig] -> the broker's park episode key "<sig>:<onset>" for a
# transition-log event, or empty when no episode is substantiable. <sig> is the caller's
# already-captured park signature; absent, it is read from the PERSISTED park-sig record (a
# cheap file read), so the shadow path never pays an extra capture-pane. Both the passed sig and
# the persisted one ARE _broker_park_signature's hash, so the recorded episode keys on the very
# signature the broker's own re-answer/onset state does (the "episode key matches the broker's
# signature" contract). onset is the live park-onset epoch (empty when none).
_gb_episode_key() {
  local issue="$1" sig="${2:-}" onset stored
  command -v read_park_onset_epoch >/dev/null 2>&1 || return 0
  if [ -z "$sig" ]; then
    stored="$(read_park_sig "$issue" 2>/dev/null)"
    sig="${stored##*$'\t'}"
  fi
  [ -n "$sig" ] || return 0
  onset="$(read_park_onset_epoch "$issue" 2>/dev/null)"
  printf '%s:%s\n' "$sig" "$onset"
}

# _gb_tlog_event <issue> <event> <lane> <episode> [evidence-json] -> record one broker lane
# event. Best-effort: no-ops when the transition-log lib is unavailable (the #300 contract) or
# the issue is not numeric (the wt_tlog_event wrapper enforces both).
_gb_tlog_event() {
  command -v wt_tlog_event >/dev/null 2>&1 || return 0
  wt_tlog_event "$1" "$2" gate-broker.sh "$3" "$4" "${5:-}"
}

# _gb_lane_event <issue> <event> <lane> <sig> [evidence-json] -> the guard-first front the wiring
# calls: the transition-log guard runs BEFORE the episode is derived, so when the lib is absent
# the shadow path is a STRICT no-op (not even the persisted-park-sig file read fires). <sig> is
# the caller's captured park signature (empty ⇒ derive from the persisted record).
_gb_lane_event() {
  command -v wt_tlog_event >/dev/null 2>&1 || return 0
  _gb_tlog_event "$1" "$2" "$3" "$(_gb_episode_key "$1" "$4")" "${5:-}"
}

# --- decision journal + warn-and-continue (issue #241) ------------------------
# The /afk answerer ALWAYS answers: every former terminal stop site (escalate-blocked, reap,
# ceiling, void, inject-failure, dispatch/land/auth halts) now TAKES the best action, WARNS
# loudly to four surfaces (drain log + hub-notify ping + --status + this decision journal),
# and parks the spoke LAST on the warned-retry backoff — never abandoned. The journal is the
# post-adjust surface: the operator reads it in the morning and reverses whatever was wrong.

# _broker_journal_file -> the per-run decision journal (one JSON line per taken decision).
_broker_journal_file() { printf '%s\n' "$(_afk_state_dir)/decision-journal.jsonl"; }

# _broker_json_escape <s> -> escape a value for a JSON string literal. A decision/reason can
# be built from captured tool output (git/gh/build lines carry \r, \t, and other C0 controls),
# and JSON forbids raw control characters in a string — so escape \ and ", space out the
# common whitespace for readability, then DROP any remaining C0 byte so the journal line stays
# valid JSONL a strict parser accepts. LC_ALL=C makes the byte range literal on this non-C host.
_broker_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"   # backslashes first, else the quote-escapes below get doubled
  s="${s//\"/\\\"}"
  s="${s//$'\t'/ }"; s="${s//$'\n'/ }"; s="${s//$'\r'/ }"   # keep the record one physical line
  printf '%s' "$s" | LC_ALL=C tr -d '\000-\037'
}

# _broker_journal_line <issue> <park_kind> <decision> <reversibility> [reasoning_ref] -> append
# ONE structured JSONL record (ts, issue, park, decision, reversibility, reasoning_ref) to the
# per-run journal FILE — and nothing else. This is the cheap, no-noise audit surface: a routine
# successful answer journals here WITHOUT a GitHub comment (per-answer comments would be spam).
# Best-effort; never aborts the caller.
_broker_journal_line() {
  local issue="$1" park="$2" decision="$3" rev="${4:-unknown}" ref="${5:-}" f
  f="$(_broker_journal_file)"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '{"ts":%s,"issue":"%s","park":"%s","decision":"%s","reversibility":"%s","reasoning_ref":"%s"}\n' \
    "$(afk_now)" "$(_broker_json_escape "$issue")" "$(_broker_json_escape "$park")" \
    "$(_broker_json_escape "$decision")" "$(_broker_json_escape "$rev")" \
    "$(_broker_json_escape "$ref")" >>"$f" 2>/dev/null || true
}

# _broker_save_reasoning <issue> <text> -> persist the reasoner's OWN output under the afk
# state dir and ECHO the path (empty when there is nothing to save or the write fails).
#
# The journal's reasoning_ref field has existed since #241, but every caller passed 4 args, so
# every record carried reasoning_ref:"" — including the four "injected answer (routine)" records
# the #271 answerer wrote while telling a spoke to abandon its correctly-pushed branch. Its
# reasoning existed nowhere on disk, so the diagnosis had to be rebuilt from the spoke pane.
# The journal says WHAT was decided; this says WHY, which is the half a morning review needs to
# tell a wrong answer from a wrongly-recorded one.
#
# Best-effort, like every helper on this path: a failed write costs the audit trail, never the
# answer — the caller journals an empty ref exactly as it did before. The filename carries the
# issue and the epoch so a re-answered gate keeps each attempt.
# UPGRADE: two answers for the same issue within ONE second collide on the filename and the
# later overwrites the earlier; the re-answer ceiling paces attempts minutes apart, so this is
# not reachable today — add a counter/pid suffix if that pacing ever goes.
_broker_save_reasoning() {
  local issue="$1" text="$2" dir f
  [ -n "$text" ] || return 0
  dir="$(_afk_state_dir)/reasoning"
  mkdir -p "$dir" 2>/dev/null || return 0
  f="$dir/$issue-$(afk_now).txt"
  printf '%s\n' "$text" >"$f" 2>/dev/null || return 0
  printf '%s\n' "$f"
}

# broker_journal_decision <issue> <park_kind> <decision> <reversibility> [reasoning_ref] ->
# journal the record (file) AND post a best-effort GitHub issue comment, so the morning review
# reads either surface. Used for NOTEWORTHY decisions (a warned/parked call, a WARN-flagged or
# non-reversible answer) — a routine successful answer uses _broker_journal_line (file only).
# reversibility is one of reversible|outward|scope|irreversible|unknown. Best-effort; never aborts.
broker_journal_decision() {
  local issue="$1" park="$2" decision="$3" rev="${4:-unknown}"
  _broker_journal_line "$@"
  _broker_journal_gh_comment "$issue" "$park" "$decision" "$rev"
  # #300 step 3b: the #277 plan-restatement fast-path is the ONLY decision journaled with park
  # kind `gate` (the answer/permission/ceiling kinds journal under their own names) — surface it
  # as a `waived` lane event so a detector reads the auto-approval explicitly. Shadow, best-effort.
  [ "$park" = gate ] && _gb_lane_event "$issue" waived gate "" \
    "{\"rev\":\"$(_broker_json_escape "$rev")\"}"
  return 0
}

# _broker_journal_gh_comment <issue> <park> <decision> <rev> -> best-effort issue comment
# recording the taken decision (#241 §10). Opt-out via AFK_JOURNAL_GH_COMMENT=0; no-op when
# gh is absent. Never aborts.
_broker_journal_gh_comment() {
  [ "${AFK_JOURNAL_GH_COMMENT:-1}" = 0 ] && return 0
  command -v gh >/dev/null 2>&1 || return 0
  local issue="$1" park="$2" decision="$3" rev="$4" body
  # Wrap the decision in backticks: a decision containing `#123` or `@name` would otherwise
  # render as a cross-issue link / user mention on GitHub, back-referencing unrelated issues.
  body="AFK auto-decision [$rev] on the $park park: \`$decision\` (review and post-adjust if wrong)"
  # Route through the TIME-BOUNDED runner so a hung gh (a black-hole network) can never
  # freeze the servicing tick — this is on the synchronous answer path. _wt_gh_run bounds
  # gh at AI_TOOLKIT_GH_TIMEOUT and returns its real rc (which we discard). Fall back to a
  # raw best-effort gh only when worktree-lib.sh did not source (the helper is undefined).
  if command -v _wt_gh_run >/dev/null 2>&1; then
    _wt_gh_run issue comment "$issue" --body "$body" || true
  else
    gh issue comment "$issue" --body "$body" >/dev/null 2>&1 || true
  fi
  return 0
}

# _broker_warned_record <issue> -> the durable, human-facing warned record: "<ts>\t<reason>".
# --status surfaces it and hub-notify pings on it (re-fired on an interval, unlike the
# once-deduped blocked ping). Distinct from blocked-<issue>.txt so the two states never blur.
_broker_warned_record() { printf '%s\n' "$(_afk_state_dir)/warned-$1.txt"; }

# broker_warn <issue> <reason> -> the loud, repeatable WARNING surface: log a WARNING line and
# overwrite the durable warned record (latest warning wins). Best-effort; never aborts.
broker_warn() {
  local issue="$1" reason="$2" f
  reason="${reason//$'\n'/ }"; reason="${reason//$'\r'/ }"   # keep the record one line (hub-notify cut -f2-)
  log "  WARNING: #$issue $reason"
  f="$(_broker_warned_record "$issue")"
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\n' "$(afk_now)" "$reason" >"$f" 2>/dev/null || true
  return 0
}

# _afk_warned_state_file <issue> [lane] -> the backoff bookkeeping: "<attempt>\t<next_retry_epoch>".
# The backoff is keyed per (issue, LANE) so one pass's warns never pace another's (#274, the same
# cross-pass-leak family as #241). An empty lane is the default ANSWER/service lane and keeps the
# historical "warned-state-<issue>" name; a named lane (e.g. "land") gets its own suffixed file so
# the answer lane's re-answer ceiling cannot starve auto_land of a ready spoke (#269).
_afk_warned_state_file() {
  local issue="$1" lane="${2:-}"
  if [ -n "$lane" ]; then printf '%s\n' "$(_afk_state_dir)/warned-state-$issue-$lane"
  else printf '%s\n' "$(_afk_state_dir)/warned-state-$issue"; fi
}

# _afk_warned_lane <park_kind> -> the backoff lane a park kind paces. auto_land is the only
# land-lane consumer: its OWN park kinds (land, review) pace the LAND lane so a land failure
# throttles future LAND attempts only; every other kind (answer/reap/dispatch/permission/ceiling)
# stays on the default lane, where an answerer's re-answer backoff belongs (#274).
_afk_warned_lane() {
  case "$1" in land | review) printf 'land\n' ;; *) ;; esac
}

# _afk_warned_lane_cap <park_kind> -> the backoff CAP a park kind uses (empty => the default
# AFK_WARN_BACKOFF_CAP). Land-lane membership is derived from _afk_warned_lane so "which kinds are
# land-lane" lives in ONE place (#274 review). A land-lane kind caps at AFK_LAND_BACKOFF_CAP
# (default 600s), deliberately BELOW the watchdog land ceiling (HUB_WATCHDOG_LAND_CEILING, 900s),
# so a done spoke's land is always re-attempted before the watchdog escalates (#274 AC4). A
# NON-NUMERIC override falls back to 600 HERE — not to _afk_warned_arm's 1800s answer default — so
# an AFK_LAND_BACKOFF_CAP typo (e.g. "15m") can never silently re-invert the ceiling (#274 review).
_afk_warned_lane_cap() {
  local cap
  [ "$(_afk_warned_lane "$1")" = land ] || return 0
  cap="${AFK_LAND_BACKOFF_CAP:-600}"; case "$cap" in '' | *[!0-9]*) cap=600 ;; esac
  printf '%s\n' "$cap"
}

# _afk_warned_arm <issue> [lane] [cap] -> advance the warned-retry backoff for one lane: read the
# prior attempt count (0 if none), schedule the next retry at now + min(BASE * 2^attempt, CAP), and
# persist "<attempt+1>\t<next>". Exponential so a standing failure is retried ever more rarely. An
# empty cap falls back to AFK_WARN_BACKOFF_CAP (the land lane passes a lower cap, #274 AC4).
#
# #318 (#300 step 6): the LAND lane additionally RECORDS the arm on the transition log, and that
# record — not the file — is what paces auto_land (see _afk_warned_next). The file is still
# written for BOTH lanes: the answer lane reads it whole, and the land lane keeps it as a pure
# PROJECTION for _afk_warn_attempt (hub-afk-recover.sh, out of #318's scope), which reads the
# attempt field for #305's warn-escalate bound. One writer either way — this function.
_afk_warned_arm() {
  local issue="$1" lane="${2:-}" cap_override="${3:-}" f base cap attempt=0 delay now i=0 next
  base="${AFK_WARN_BACKOFF_BASE:-60}"; case "$base" in '' | *[!0-9]*) base=60 ;; esac
  cap="${cap_override:-${AFK_WARN_BACKOFF_CAP:-1800}}"; case "$cap" in '' | *[!0-9]*) cap=1800 ;; esac
  f="$(_afk_warned_state_file "$issue" "$lane")"
  if [ -f "$f" ]; then IFS=$'\t' read -r attempt _ <"$f" 2>/dev/null || true; fi
  case "$attempt" in '' | *[!0-9]*) attempt=0 ;; esac
  delay="$base"
  while [ "$i" -lt "$attempt" ] && [ "$delay" -lt "$cap" ]; do delay=$(( delay * 2 )); i=$(( i + 1 )); done
  [ "$delay" -gt "$cap" ] && delay="$cap"
  now="$(afk_now)"
  next=$(( now + delay ))
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  printf '%s\t%s\n' "$(( attempt + 1 ))" "$next" >"$f" 2>/dev/null || true
  # The arm RECORDS the epoch it just computed rather than leaving a reader to re-derive the
  # curve from the event's timestamp: the math has one writer, so no reader can drift from it
  # (the single-parse-site rationale _afk_warned_next already carries), and the recorded epoch
  # honors afk_now — the log's own ts is a real `date +%s`, which would ignore a pinned clock.
  [ "$lane" = land ] \
    && _afk_tlog_land_event "$issue" land_failed "{\"attempt\":$(( attempt + 1 )),\"next\":$next}"
  return 0
}

# The land backoff's pacing records live on their OWN lane, not on `land`.
#
# Lane `land` is ALREADY written by broker_warn_continue, which records an `escalated` event for
# every land/review park right after arming the backoff (see below). Putting the pacing records
# there too would give one lane two writers with two meanings — and the reader below, which takes
# the lane's LAST event, would read that `escalated` (no `next` key) as "never armed" and re-run
# an expensive land every tick. That is AFK Principle 5 exactly: a lane means ONE thing, set by
# ONE writer. So the backoff gets `land-backoff`, whose only events are this module's own arm and
# clear — which makes "the last record wins" the whole truth about pacing.
_AFK_LAND_BACKOFF_LANE=land-backoff

# _afk_tlog_land_event <issue> <event> <evidence-json> -> record one land-backoff pacing decision.
# Best-effort by the #300 contract: no-ops when the log is unavailable and never fails the caller.
_afk_tlog_land_event() {
  command -v wt_tlog_event >/dev/null 2>&1 || return 0
  wt_tlog_event "$1" "$2" hub-afk.sh "$_AFK_LAND_BACKOFF_LANE" "" "$3" || true
}

# _afk_warned_next <issue> [lane] -> echo the lane's next-due epoch (empty when never armed). The
# single reader of the backoff record's <next> field: _afk_warned_due gates on it and the auto_land
# skip log names it, so a paced land is visible, not a silent continue (#274 AC3). One parse site so
# the two callers can never diverge on the record format (#274 review).
#
# #318: the LAND lane reads its epoch from the transition log — the lane's own recorded arm — so
# auto_land paces on a recorded fact, visible to anything reading the lifecycle log, instead of a
# side-channel file only the drain could see. Every other lane still reads the file.
_afk_warned_next() {
  local issue="$1" lane="${2:-}" f next=""
  if [ "$lane" = land ] && command -v afk_lane_last_event >/dev/null 2>&1; then
    _afk_land_next_from_log "$issue"
    return 0
  fi
  f="$(_afk_warned_state_file "$issue" "$lane")"
  [ -f "$f" ] || return 0
  IFS=$'\t' read -r _ next <"$f" 2>/dev/null || true
  printf '%s\n' "$next"
}

# _afk_land_next_from_log <issue> -> the land lane's next-due epoch as its last recorded event
# states it; empty when the lane has no record at all, or when that record is a CLEAR — which
# deliberately carries no `next` key, so it reads back as exactly the "never armed" the retired
# file expressed by not existing. Both mean due now.
#
# The epoch is read from the event's EVIDENCE, which is safe here precisely because it is this
# module's own single-writer record (_afk_tlog_land_event) on a lane nothing else writes: the
# `next` key is ours, and the builders append evidence strictly LAST, so no top-level field can
# be confused for it. A non-numeric parse degrades to empty = due, never to a stall.
_afk_land_next_from_log() {
  local line next
  line="$(afk_lane_last_event "$1" "$_AFK_LAND_BACKOFF_LANE" 2>/dev/null)"
  [ -n "$line" ] || return 0
  next="$(printf '%s' "$line" | sed -n -E 's/.*"next":([0-9]+).*/\1/p')"
  case "$next" in '' | *[!0-9]*) return 0 ;; esac
  printf '%s\n' "$next"
}

# _afk_warned_due <issue> [now] [lane] -> rc 0 when the spoke is due for a retry on that lane
# (never warned, or the backoff window has elapsed), rc 1 when still inside the backoff (parked
# LAST this tick). An empty lane reads the default (answer/service) lane.
_afk_warned_due() {
  local issue="$1" now="${2:-$(afk_now)}" lane="${3:-}" next
  next="$(_afk_warned_next "$issue" "$lane")"
  case "$next" in '' | *[!0-9]*) return 0 ;; esac
  [ "$now" -ge "$next" ]
}

# _afk_clear_warned <issue> -> drop one spoke's warned record + backoff for EVERY lane (called on
# genuine progress: a tip advance or a fresh marker means the warned state is stale). Clears both
# the default (answer) lane and the land lane so no stale record in either keeps pacing (#274).
#
# #318: the land lane's pacing record lives in an APPEND-ONLY log, so its clear cannot be a
# delete — it is RECORDED (Principle 1: a reset is a fact, not an absence). Skipping the record
# would leave a genuine-progress clear paced by a stale arm for the rest of the window: a land
# that silently never retries, the #299 shape. Gated on the projection file's presence so the
# log gets ONE clear per arm cycle rather than one per progress tick — _afk_clear_warned fires
# on every tip advance for every spoke, and that file existing is exactly "this lane is armed".
_afk_clear_warned() {
  local issue="$1"
  [ -f "$(_afk_warned_state_file "$issue" land)" ] \
    && _afk_tlog_land_event "$issue" land_cleared '{"cleared":true}'
  rm -f "$(_afk_warned_state_file "$issue")" "$(_afk_warned_state_file "$issue" land)" \
        "$(_broker_warned_record "$issue")" 2>/dev/null || true
  return 0
}
# _clear_warned_records -> drop every warned record + backoff for a freshly-armed window.
#
# #318: the land lane's pacing is a RECORD in an append-only log, so the glob delete below cannot
# reach it — a fresh window would inherit the last arm's cadence (up to the land cap), and
# _clear_progress_state's contract is that a fresh window inherits none. So record a clear for
# every armed land lane first. The projection files are exactly the per-issue armed set, so this
# needs no worktree enumeration; an unmatched glob falls through the -f guard.
_clear_warned_records() {
  local dir f issue; dir="$(_afk_state_dir)"
  for f in "$dir"/warned-state-*-land; do
    [ -f "$f" ] || continue
    issue="${f##*/warned-state-}"; issue="${issue%-land}"
    _afk_tlog_land_event "$issue" land_cleared '{"cleared":true,"reason":"fresh afk window"}'
  done
  rm -f "$dir"/warned-*.txt "$dir"/warned-state-* 2>/dev/null || true
}

# broker_warn_continue <wt> <issue> <park_kind> <decision> <reversibility> -> the #241
# replacement for _escalate_blocked at a converted stop site: warn loudly, journal the taken
# decision, advance the backoff, emit a warn span, and RETURN — the spoke stays in rotation
# (no blocked tag, no pane kill). It is retried on the backoff until it makes progress.
broker_warn_continue() {
  local wt="$1" issue="$2" park="$3" decision="$4" rev="${5:-unknown}"
  broker_warn "$issue" "$decision"
  broker_journal_decision "$issue" "$park" "$decision" "$rev"
  # Arm the lane the park kind belongs to (#274): a land/review park paces auto_land's LAND lane
  # (capped below the watchdog ceiling); every other kind paces the default answer/service lane.
  _afk_warned_arm "$issue" "$(_afk_warned_lane "$park")" "$(_afk_warned_lane_cap "$park")"
  afk_emit_decision "$wt" warn
  # #300 step 3b: a warn-and-continue on the ANSWER lane means the computed answer reached no
  # delivery — a dropped answer, recorded WITH its reason; every other park kind's fallback is an
  # escalation. Shadow-only, episode from the persisted park-sig (no pane read).
  local _gb_ev=escalated
  [ "$park" = answer ] && _gb_ev=answer_dropped
  _gb_lane_event "$issue" "$_gb_ev" "$park" "" \
    "{\"rev\":\"$(_broker_json_escape "$rev")\",\"reason\":\"$(_broker_json_escape "$decision")\"}"
  return 0
}

# _rule_file -> the afk-answering rule path, across both layouts; empty if unfound.
_rule_file() {
  local cand
  for cand in \
    "${AFK_RULE_FILE:-}" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/.claude/rules/afk-answering.md" \
    "$SCRIPT_DIR/../../../../shared/rules/afk-answering.md" \
    "${MAIN_ROOT:-$_AFK_TOPLEVEL}/shared/rules/afk-answering.md"; do
    [ -n "$cand" ] && [ -f "$cand" ] && { printf '%s\n' "$cand"; return 0; }
  done
  return 1
}

# build_answerer_prompt <issue> <question> -> the full prompt for the reasoner: the
# governing rule, the issue contract, the read-only-worktree posture + evidence contract,
# a decisions-digest of this spoke's prior gate outcomes, and the parked prompt.
# Self-contained so the headless reasoner needs no project context loaded. The reasoner's
# cwd is the #237 snapshot COPY (created in run_answerer before this is called), so the
# posture points at "a throwaway copy (your cwd)" and — deliberately (#239) — never
# discloses the live worktree's absolute path, which used to invite an absolute-path write
# into the real tree.
# _default_answerer_policy -> the built-in fallback policy shipped when the afk-answering rule
# file is absent. #241: the reasoner ALWAYS answers — it never escalates-and-parks. It is kept
# in lockstep with shared/rules/afk-answering.md by a binding test, so both surfaces retire the
# ESCALATE output token and both instruct the ANSWER + REVERSIBILITY lines.
_default_answerer_policy() {
  cat <<'POLICY'
Answer in the interest of the issue contract and repo conventions; prefer the spoke's own
recommended option. You ALWAYS answer — you never escalate and park the spoke for a human.
For an irreversible, outward-facing, or scope-changing ask, choose the REVERSIBLE, in-scope
alternative when one exists (that IS the answer — e.g. do not force-push; rebase onto a new
branch instead; deny a destructive command and tell the spoke the reversible path); only when
no reversible alternative exists do you decide on the merits.
SHIP DISCIPLINE, an exception to the above: every spoke is dispatched with a standing
contract to push its OWN feature branch on every subtask and emit its ready marker once the
acceptance criteria are met, both without asking; the hub — never the spoke — lands the
issue. That push and that ready marker are therefore the expected, mandatory ship step and
are REVERSIBLE (the hub lands from origin; it is not a push to the default branch, and the
branch is deletable) — NOT outward-facing, so approve them. Never answer 'keep it local',
'do not push', 'do not emit the ready marker', or 'delete the branch' to a spoke's own
feature-branch push. Only a push/force-push to the DEFAULT branch or a genuine history
rewrite is the irreversible ask the reversible-alternative posture is for.
Precede your decision with a 'REVERSIBILITY: reversible|outward|scope|irreversible' line
naming the class, and add a
'WARN: <what the human should double-check>' line whenever you take a critical, irreversible,
outward-facing, or scope-changing decision so it is loudly recorded for morning post-review.
End with exactly one final line: 'ANSWER: <reply>'.
POLICY
}

# _broker_issue_body <issue> -> the issue's "title\n\nbody" (via gh), or a placeholder when gh
# is unavailable. Extracted from build_answerer_prompt so the reasoner prompt AND the #277
# PLAN-gate fast-path pre-check compare the posted plan against the SAME issue text — one
# source of truth, no drift between "what the reasoner sees" and "what the coverage measures".
_broker_issue_body() {
  gh issue view "$1" --json title,body -q '.title + "\n\n" + .body' 2>/dev/null \
    || echo "(issue #$1 body unavailable)"
}

# _broker_plan_is_restatement <plan> <body> -> the #277 PLAN-gate fast-path predicate. ECHO the
# coverage ratio [0.00-1.00] of the posted <plan>'s SIGNIFICANT tokens that also appear in the
# issue <body> (a bag-of-words overlap coefficient) and RETURN rc 0 when the plan is a confident
# RESTATEMENT of the body: coverage >= AFK_FASTPATH_COVERAGE (default 0.85) AND the plan carries
# >= AFK_FASTPATH_MIN_TOKENS (default 12) significant tokens (a too-short plan can't be judged a
# confident restatement, so it falls through). rc 1 = not a restatement — ALSO the no-python3
# fallback: with the coverage unknowable the fast path fails SAFE to the full reasoner. Significant
# = a lowercased [a-z0-9]+ run of length >= 3 that is not a common stopword. python3 is the broker's
# text tool (as in _read_gate_artifact); the two texts ride env vars so a large plan never hits
# argv limits.
_broker_plan_is_restatement() {
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_PLAN="$1" _AFK_BODY="$2" python3 2>/dev/null <<'PYEOF'
import os, re, sys

# Common function words plus a few plan/issue scaffolding words that appear in nearly every
# body AND plan; dropping them SYMMETRICALLY leaves coverage reflecting SUBSTANTIVE overlap.
STOP = {
    "the", "and", "for", "that", "this", "with", "from", "into", "onto", "not", "but", "are",
    "was", "will", "when", "then", "than", "its", "you", "your", "our", "does", "done",
    "per", "via", "use", "used", "uses", "add", "adds", "set", "sets", "new", "one", "two",
    "all", "any", "each", "only", "line", "lines", "file", "files", "test", "tests", "code",
    "plan", "issue", "fix", "fixes", "should", "must", "can", "here", "have", "has",
}


def toks(s):
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) >= 3 and w not in STOP}


def envnum(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


plan = toks(os.environ.get("_AFK_PLAN", ""))
body = toks(os.environ.get("_AFK_BODY", ""))
thresh = envnum("AFK_FASTPATH_COVERAGE", 0.85)
min_toks = envnum("AFK_FASTPATH_MIN_TOKENS", 12)

# Round BEFORE both the echo and the gate so the recorded number can never disagree with the
# decision at the boundary (0.846 must not print "0.85" while the reasoner actually ran).
cov = round((len(plan & body) / len(plan)) if plan else 0.0, 2)
sys.stdout.write(f"{cov:.2f}")
sys.exit(0 if (len(plan) >= min_toks and cov >= thresh) else 1)
PYEOF
}

build_answerer_prompt() {
  local issue="$1" question="$2" rule body digest
  rule="$(_rule_file)" && rule="$(cat "$rule")" \
    || rule="$(_default_answerer_policy)"
  body="$(_broker_issue_body "$issue")"
  digest="$(read_decisions_digest "$issue")"
  cat <<EOF
$rule

## Issue contract (#$issue)

$body

## Read-only worktree access

You have READ-ONLY access to a throwaway COPY of the spoke's worktree (your cwd). Use your
read/search tools to verify the decision against the code as it ACTUALLY is — confirm a command
touches only the spoke's own files, that a posted plan matches real state, and so on.
You must NOT edit, stage, commit, or push anything: the tree is read-only and any write
voids your answer. When you auto-answer, cite the worktree EVIDENCE you checked on an
'EVIDENCE:' line before your final decision line.

## Prior gate decisions for this spoke (decisions-digest)

${digest:-(none recorded yet)}

## Ship discipline (the spoke's standing contract)

This spoke was dispatched with a standing contract: push its OWN feature branch on every
subtask, and emit its ready marker once the acceptance criteria are met -- both without
asking. The hub, never the spoke, lands the issue.

A spoke's own feature-branch push and its ready marker are therefore the expected, mandatory
ship step, and they are REVERSIBLE: the hub lands from origin, it is not a push to the
default branch, and the branch is trivially deletable. They are NOT outward-facing, so
approve them. Never answer "keep it local", "do not push", "do not emit the ready marker",
or "delete the branch" to a spoke's own feature-branch push -- that countermands the
contract and strands finished work. Only a push or force-push to the DEFAULT branch, or a
genuine history rewrite, is the irreversible ask to decline.

## The spoke's parked prompt

$question

Decide per the policy above — you ALWAYS answer, never escalate-and-park. Precede your
decision with a 'REVERSIBILITY: reversible|outward|scope|irreversible' line, and a
'WARN: <what to double-check>' line for any critical, irreversible, outward-facing, or
scope-changing call. End with exactly one final line: 'ANSWER: <reply>'.
EOF
}

# --- bounding the reasoner (issue #171, subtask 1) ----------------------------
# An untimed headless `claude` can hang the whole tick; every reasoner run is bounded so a
# wedged answerer never freezes the supervisor. Expiry yields no decision line, so the gate
# fails SAFE to escalate (blocked/<issue>) — the existing no-decision fail-safe.

# _afk_answerer_timeout -> the reasoner's wall-clock budget in seconds. AFK_ANSWERER_TIMEOUT
# tunes it (default 900); a non-numeric OR non-positive override (0 disables the bound in
# both `timeout` and perl `alarm`) falls back to the default, so the cap is never silently
# lifted (#171 review).
_afk_answerer_timeout() {
  local s="${AFK_ANSWERER_TIMEOUT:-900}"
  case "$s" in '' | *[!0-9]* ) s=900 ;; esac
  [ "$s" -lt 1 ] && s=900
  printf '%s\n' "$s"
}

# _broker_run_bounded <secs> <cmd...> -> run <cmd...> (prompt on this function's stdin) under
# a <secs> wall-clock cap and return its exit code (nonzero on expiry). PREFERS hub-afk's
# shared _afk_with_timeout when the supervisor sourced it (issue #170): it tree-kills a
# wedged grandchild via _afk_kill_tree, so a hung `claude` can't keep run_answerer's capture
# pipe open and re-hang the tick. Reused (not re-implemented) via a runtime existence check —
# the same seam gate-broker uses for respawn_wedged_spoke — so the bound has one owner. Falls
# back to a self-contained bound only for a STANDALONE / attended broker without hub-afk (the
# tests): coreutils timeout/gtimeout, then a perl(alarm) wrapper (SIGALRM survives exec and
# terminates a runaway), then best-effort unbounded.
_broker_run_bounded() {
  local secs="$1"; shift
  if command -v _afk_with_timeout >/dev/null 2>&1; then _afk_with_timeout "$secs" "$@"; return; fi
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"; return; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout "$secs" "$@"; return; fi
  if command -v perl >/dev/null 2>&1; then
    # UPGRADE: unlike _afk_with_timeout this does not reap a wedged grandchild — only reached
    # in a hub-less standalone/attended context where a long-lived `claude` grandchild is not
    # expected; production routes through _afk_with_timeout above.
    perl -e 'alarm shift @ARGV; exec @ARGV or exit 127' "$secs" "$@"; return
  fi
  "$@"   # no bounding tool available — best-effort unbounded
}

# run_answerer <issue> <question> [wt] -> the reasoner's raw output (stdout AND stderr),
# and its exit status as the function's return code. The reasoner is a headless `claude
# -p` (overridable via AFK_ANSWERER_CMD for tests), run with a thinking budget and a
# READ-ONLY tool allowlist; the prompt is passed on stdin so a long contract never hits
# argv limits. When <wt> is a directory it becomes the reasoner's cwd, so its read-only
# tools verify against the spoke's live state (the mutation guard in broker_service_gate
# is what makes that safe). The run is bounded by AFK_ANSWERER_TIMEOUT (_broker_run_bounded)
# so a hung `claude` never freezes the tick; expiry reads as no decision → escalate.
# stderr is folded into the captured stream (NOT discarded)
# because the CLI prints credential failures there and exits nonzero — the auth-failure
# detector needs both the message and the exit code. parse_decision is line-anchored, so
# interleaved stderr noise never pollutes a decision.
#
# --no-session-persistence stays belt-and-suspenders for #164. The original collision: the
# reasoner ran with cwd=<wt>, so a persisted transcript landed in the SAME
# ~/.claude/projects/<munged-wt>/ dir as the spoke's own, shadowing it — `_spoke_jsonl` picks
# the newest jsonl there, so every `_still_parked_same` check saw the transcript "move" and
# dropped the answer as stale, stranding the spoke. The #237 write-isolation snapshot already
# removes that collision at the root: the reasoner's cwd is now a mktemp copy, so any persisted
# transcript maps to the copy's OWN munged dir — disjoint from <wt>'s. We keep the flag anyway
# so no throwaway transcript is written for the snapshot path at all. It does NOT touch
# CLAUDE_CONFIG_DIR, so keychain credentials/auth are unaffected.
# UPGRADE: if a deployed `claude` lacks --no-session-persistence it exits nonzero with no
# decision, so the gate fails SAFE (escalates to blocked/<issue>) rather than stranding —
# but auto-answering silently stops; drop the flag / switch to filtering the reasoner's
# jsonl out of _spoke_jsonl if the installed CLI ever loses it.
run_answerer() {
  local issue="$1" question="$2" wt="${3:-}"
  local tools; tools="$(reasoner_allowed_tools)"
  # #247 option (c): `--output-format stream-json --verbose` streams every reasoner tool_use
  # (name + input) onto the captured stdout so _reasoner_wrote_live_tree can AUDIT what the
  # reasoner actually did (the void's attribution signal). The stream is NOT the final answer:
  # every CALLER must run the raw output through _normalize_answerer_output before any text parse
  # (parse_decision / parse_decision_field / is_auth_failure) — the audit is the ONE consumer that
  # reads the raw stream. `--verbose` is required for stream-json under `-p`; a deployed CLI that
  # lacks the format exits nonzero → the audit reads no stream (rc 2) and the void degrades to the
  # #244 activity fallback — safe, never stranding.
  local cmd="${AFK_ANSWERER_CMD:-claude -p --no-session-persistence --output-format stream-json --verbose --model claude-opus-4-8 --allowedTools '$tools'}"
  local secs; secs="$(_afk_answerer_timeout)"
  # Write isolation (#237): run the reasoner against a throwaway COPY of the worktree, not the
  # spoke's LIVE tree — so even a tool that ignores the read-only allowlist writes into the
  # copy. The reasoner's cwd moves to the snapshot; broker_service_gate still fingerprints the
  # real $wt, now a should-never-fire backstop. On any copy failure (no mktemp, non-git tree),
  # fall back to running in-place: the fingerprint void remains the guard. The snapshot is
  # built BEFORE the prompt (#239) so the posture can point cwd at the copy and never disclose $wt.
  local snap="" run_dir="$wt"
  if [ -n "$wt" ] && [ -d "$wt" ]; then
    snap="$(mktemp -d 2>/dev/null)" || snap=""
    if [ -n "$snap" ] && _broker_snapshot_worktree "$wt" "$snap"; then
      run_dir="$snap"
    elif [ -n "$snap" ]; then
      rm -rf "$snap" 2>/dev/null || true; snap=""
    fi
  fi
  local prompt; prompt="$(build_answerer_prompt "$issue" "$question")"
  # Deliver the prompt via a temp file the wrapped command re-opens with `exec <`, NOT only
  # the here-string: the bound (_afk_with_timeout's portable fallback) BACKGROUNDS the
  # command, and POSIX assigns a backgrounded job's stdin to /dev/null — a plain here-string
  # would be lost, starving the reasoner of its prompt. `exec <file` reopens stdin inside the
  # backgrounded shell, so the prompt survives every bound path. The here-string stays as a
  # fallback for when mktemp is unavailable (the foreground timeout/perl paths keep stdin).
  local pf rc; pf="$(mktemp 2>/dev/null)" || pf=""
  [ -n "$pf" ] && { printf '%s' "$prompt" > "$pf"; cmd="exec <'$pf'; $cmd"; }
  # _broker_run_bounded caps the reasoner (#171): a hung `claude` never freezes the tick.
  # stderr is folded in (2>&1) so the auth-failure detector still sees credential messages.
  (
    [ -n "$run_dir" ] && [ -d "$run_dir" ] && cd "$run_dir"
    CLAUDE_EFFORT="$AFK_ANSWERER_EFFORT" _broker_run_bounded "$secs" bash -c "$cmd" <<<"$prompt" 2>&1
  )
  rc=$?
  [ -n "$pf" ] && rm -f "$pf"
  [ -n "$snap" ] && rm -rf "$snap" 2>/dev/null || true
  # #300 step 3b: the reasoner just computed a decision — record it. lane defaults to the answer
  # lane; the permission reasoner runs us under AFK_TLOG_LANE=permission so its own compute is
  # labelled correctly. Shadow-only, episode from the persisted park-sig (no pane read).
  _gb_lane_event "$issue" answer_computed "${AFK_TLOG_LANE:-answer}" "" "{\"rc\":$rc}"
  return "$rc"
}

# _normalize_answerer_output <raw> -> the reasoner's FINAL TEXT, extracted from a
# `--output-format stream-json` event stream (#247) so the line-anchored DECISION parsers
# (parse_decision / parse_decision_field) see the ANSWER / REVERSIBILITY / WARN lines they
# expect — NOT buried inside JSON, where they would silently read as empty and drop the #241
# reversibility class + WARN note. (is_auth_failure is fed the RAW stream instead, so an auth
# signature carried in a dropped event — a system/error line — is never missed; see the call
# sites.) The extraction:
#   - the final `type:"result"` event's `result` field (the consolidated answer) wins; a
#     missing/empty result falls back to concatenated assistant `text` blocks (real claude emits
#     BOTH, so the answer survives a drift in either shape);
#   - NON-JSON lines pass through (a plain-text answerer stub is entirely non-JSON, so it is
#     surfaced whole).
# The raw stream is delivered via a temp FILE (path in env), never argv/env directly, so a verbose
# stream echoing large tool_result payloads never trips ARG_MAX. A pure plain-text input (every
# #244 answerer stub) has no JSON events, so its content passes through — the DECISION lines are
# preserved, though the python path normalizes whitespace (drops blank lines, adds a trailing
# newline); only the no-python3 branch is byte-identical. No python3 ⇒ byte-for-byte passthrough
# (the degraded env keeps plain-text answerers working; the void degrades to the #244 fallback).
_normalize_answerer_output() {
  command -v python3 >/dev/null 2>&1 || { printf '%s' "$1"; return 0; }
  local rawfile; rawfile="$(mktemp 2>/dev/null)" || { printf '%s' "$1"; return 0; }
  printf '%s' "$1" > "$rawfile"
  _AFK_RAWFILE="$rawfile" python3 2>/dev/null <<'PYEOF' || printf '%s' "$1"
import json, os

result_text = None
assistant_texts = []
passthrough = []
with open(os.environ["_AFK_RAWFILE"], encoding="utf-8", errors="replace") as fh:
    for raw_line in fh:
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            passthrough.append(raw_line.rstrip("\n"))  # plain text (a stub) — surfaced whole
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "result":
            r = obj.get("result")
            if isinstance(r, str) and r.strip():
                result_text = r  # the LAST result event's consolidated text wins
        elif kind == "assistant":
            content = (obj.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and (block.get("text") or "").strip():
                        assistant_texts.append(block["text"])
        # other event types (system/user/tool_use noise) carry no final text — skipped.

out = []
if result_text is not None:
    out.append(result_text)
elif assistant_texts:
    out.append("\n".join(assistant_texts))
out.extend(passthrough)
print("\n".join(out))
PYEOF
  rm -f "$rawfile" 2>/dev/null || true
}

# parse_decision <raw-answerer-output> -> "ANSWER\t<text>" or "ESCALATE\t<reason>" on
# stdout, or empty when the answerer emitted no decision line. The LAST matching line
# wins (the answerer reasons first, then concludes). Decisions are SINGLE-LINE by
# construction (the grep is line-anchored) — inject_answer and _afk_continue_command
# rely on this; supporting multi-line answers would re-trigger the bracketed-paste
# hazard (#123/#124) and the quoting hazard on the respawn command line.
parse_decision() {
  local line kind rest
  line="$(printf '%s\n' "$1" | grep -E '^(ANSWER|ESCALATE):' | tail -1)"
  [ -n "$line" ] || return 0
  kind="${line%%:*}"
  rest="${line#*:}"
  rest="${rest#"${rest%%[![:space:]]*}"}"          # ltrim
  printf '%s\t%s\n' "$kind" "$rest"
}

# parse_decision_field <raw-answerer-output> <KEYWORD> -> the trimmed value of the LAST
# '<KEYWORD>: <value>' line (empty when absent). #241 reads the reasoner's 'REVERSIBILITY:'
# class and 'WARN:' note off the same single-line convention as the ANSWER line, so a taken
# decision carries its reversibility class + human-review flag into the decision journal.
# <KEYWORD> must be a metacharacter-free literal (callers pass REVERSIBILITY / WARN); it is
# interpolated into an ERE. The value is both l- and r-trimmed so a class enum compares exact.
parse_decision_field() {
  local raw="$1" key="$2" line rest
  line="$(printf '%s\n' "$raw" | grep -E "^${key}:" | tail -1)"
  [ -n "$line" ] || return 0
  rest="${line#*:}"
  rest="${rest#"${rest%%[![:space:]]*}"}"          # ltrim
  rest="${rest%"${rest##*[![:space:]]}"}"          # rtrim
  printf '%s\n' "$rest"
}

# is_auth_failure <raw-answerer-output> -> true (rc 0) when the text carries a Claude /
# Anthropic auth-failure signature (dead credentials / token could not refresh). Matched
# case-insensitively against the known wordings. The CALLER additionally gates on the
# answerer having EXITED NONZERO (decide_and_act) — auth discussion in a healthy answer
# exits 0 and is never treated as a failure — so this predicate can favor recall without
# a false positive halting the whole drain. The /login signature is still anchored to the
# CLI's "run [`claude `]/login" phrasing so prose like "run the /login migration" misses.
is_auth_failure() {
  printf '%s' "$1" | grep -Eqi \
    'authentication_error|invalid (x-)?api[ -]?key|invalid bearer token|oauth (token|authentication)|run `?(claude )?/login|401|unauthorized|credit balance is too low'
}
