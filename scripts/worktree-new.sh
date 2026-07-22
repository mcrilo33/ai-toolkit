#!/usr/bin/env bash
#
# worktree-new.sh — create an isolated git worktree for one task and wire it into
# a "multiple terminals + one review window" workflow:
#   - folds the worktree into your single VS Code review window (`code --add`)
#   - opens a tmux window cd'd into it in the project's session, launching `claude`
#
# One task = one issue = one branch = one checkout = its own staging area, hooks,
# and .review/ approval artifacts (the isolation solo-cycle/close-task assume).
#
# Usage:
#   scripts/worktree-new.sh <issue> [slug] [type] [flags]
#
#   <issue>  GitHub issue number (or a bare slug for ad-hoc work)
#   [slug]   short branch slug; derived from the issue title when omitted (needs gh)
#   [type]   feature | fix | chore   (default: feature)
#
#   -t, --type <t>   branch type (feature|fix|chore) — unambiguous, beats the
#                    positional [type] slot
#   --prompt <text>  seed the spawned claude with this first message (e.g. /source
#                    or a task kickoff) — used by the start-task skill to dispatch
#   --mode <m>       execution mode stamped on the trace (attended|afk; default
#                    attended) — hub-afk.sh passes `afk` for drain-driven spokes (#102)
#   --new-window     open a SEPARATE VS Code window instead of code --add
#   --no-code        don't touch VS Code
#   --no-terminal    don't spawn a tmux/terminal window
#   --no-agent       spawn the terminal but don't launch `claude` in it
#
# Env: WT_AGENT_MODEL / WT_AGENT_EFFORT pin the spawned agent's model and effort
#      (defaults: opus / max).
#
# Examples:
#   scripts/worktree-new.sh 42                          # feature/42-<title>, review window + tmux
#   scripts/worktree-new.sh 57 null-pointer fix
#   scripts/worktree-new.sh refactor-sync -t chore      # chore/refactor-sync (ad-hoc + type)
#   scripts/worktree-new.sh 42 --prompt "/source"       # spoke starts anchored to the issue
#
set -euo pipefail

WT_PROG="worktree-new"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=worktree-lib.sh
. "$SCRIPT_DIR/worktree-lib.sh"

# Span start clock for the lifecycle/spawn span emitted at the end.
WT_T0="$(wt_now_ms)"

# --- parse flags vs positionals ----------------------------------------------
POSITIONAL=()
OPEN_MODE="add"        # add | new-window | none
SPAWN_TERMINAL=1
LAUNCH_AGENT=1
PROMPT=""              # seed the spawned claude with this first message
TYPE_FLAG=""           # --type overrides the positional type (no footgun)
MODE="attended"        # execution mode stamped on the trace (attended | afk); #102
# --subtasks N,M: extra issues this ONE spoke ships (#278). NOT named SUBTASKS: that name is
# already taken further down for the ledger skeleton's body-derived subtask list, which would
# silently clobber this one.
SUBTASK_ARG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --new-window)  OPEN_MODE="new-window"; shift ;;
    --no-code)     OPEN_MODE="none"; shift ;;
    --no-terminal) SPAWN_TERMINAL=0; shift ;;
    --no-agent)    LAUNCH_AGENT=0; shift ;;
    -t|--type)     [ "$#" -ge 2 ] || wt_die "--type needs a value"; TYPE_FLAG="$2"; shift 2 ;;
    --type=*)      TYPE_FLAG="${1#--type=}"; shift ;;
    --prompt)      [ "$#" -ge 2 ] || wt_die "--prompt needs a value"; PROMPT="$2"; shift 2 ;;
    --prompt=*)    PROMPT="${1#--prompt=}"; shift ;;
    --mode)        [ "$#" -ge 2 ] || wt_die "--mode needs a value"; MODE="$2"; shift 2 ;;
    --mode=*)      MODE="${1#--mode=}"; shift ;;
    # --subtasks N,M (#278): the extra issues batch-plan packed onto THIS spoke. They ride
    # the primary's branch as ordered subtasks instead of each paying a full spoke lifecycle.
    --subtasks)    [ "$#" -ge 2 ] || wt_die "--subtasks needs a value"; SUBTASK_ARG="$2"; shift 2 ;;
    --subtasks=*)  SUBTASK_ARG="${1#--subtasks=}"; shift ;;
    -*)            wt_die "unknown option: $1" ;;
    *)             POSITIONAL+=("$1"); shift ;;
  esac
done

[ "${#POSITIONAL[@]}" -ge 1 ] || wt_die "usage: worktree-new.sh <issue> [slug] [type] [flags]"
ISSUE="${POSITIONAL[0]}"
SLUG_ARG="${POSITIONAL[1]:-}"
TYPE="${TYPE_FLAG:-${POSITIONAL[2]:-feature}}"   # --type wins, else 3rd positional, else feature

case "$TYPE" in
  feature|fix|chore) ;;
  *) wt_die "type must be one of: feature, fix, chore (got '$TYPE')" ;;
esac

# --- locate the main checkout ------------------------------------------------
# Resolve the MAIN worktree root, so creating from inside an existing worktree
# still places siblings next to the real checkout (not next to a worktree).
git rev-parse --git-dir >/dev/null 2>&1 || wt_die "run this from inside your checkout (cd into the repo first)"
REPO_ROOT="$(wt_main_root)" || wt_die "could not locate the main worktree"
cd "$REPO_ROOT"

# --- derive slug + branch ----------------------------------------------------
# An explicitly-passed slug is still slugified, so spaces or odd characters can
# never produce an invalid git ref.
if [ -n "$SLUG_ARG" ]; then
  SLUG="$(wt_slugify "$SLUG_ARG")"
elif [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
  if command -v gh >/dev/null 2>&1; then
    TITLE="$(gh issue view "$ISSUE" --json title -q .title 2>/dev/null || true)"
    if [ -n "$TITLE" ]; then
      SLUG="$(wt_slugify "$TITLE")"
    else
      wt_warn "could not fetch issue #$ISSUE title (gh failed, not authed, or no such issue);"
      wt_warn "falling back to a slug from the number — pass an explicit slug to override."
      SLUG="$(wt_slugify "$ISSUE")"
    fi
  else
    wt_warn "gh not found; cannot fetch issue #$ISSUE title — using the number as the slug."
    SLUG="$(wt_slugify "$ISSUE")"
  fi
else
  SLUG="$(wt_slugify "$ISSUE")"
fi
[ -n "$SLUG" ] || wt_die "could not derive a branch slug; pass one explicitly"

# Branch: feature/<id>-<slug> for numeric issues, <type>/<slug> for ad-hoc.
# (This convention is what source-task / solo-cycle / commit-quality expect —
# which is why we keep the script instead of native `claude -w`'s worktree-<name>.)
if [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
  BRANCH="${TYPE}/${ISSUE}-${SLUG}"
  WT_TAG="$ISSUE"
  LANE="spoke"            # issue-backed full solo-cycle (#102)
else
  BRANCH="${TYPE}/${SLUG}"
  WT_TAG="$SLUG"
  LANE="express"          # ad-hoc, no-issue express spoke (#102)
fi

# --- packed subtasks (issue #278) --------------------------------------------
# batch-plan emits an ordered GROUP per spoke ("263,265 270"): same-scope issues that could
# never run concurrently ride ONE branch as ordered subtasks, instead of each paying the
# whole spoke lifecycle tax (worktree, first-push suite seed 12-47 min, PLAN gate, review,
# land). The branch keeps leading with the PRIMARY — inflight_worktrees and worktree-land
# both parse the issue out of the leading digits of the branch slug — and the extra issues
# go into the queued-subtask channel, which is what holds the spoke's terminal
# ready/<primary> until it has shipped them all.
#
# SUBTASK_LIST is normalized here (validated, primary dropped, deduped) so the seed prompt
# below and the queue seeding agree on one list.
SUBTASK_LIST=()
if [ -n "$SUBTASK_ARG" ]; then
  [[ "$ISSUE" =~ ^[0-9]+$ ]] || wt_die "--subtasks needs a numbered primary issue (got '$ISSUE')"
  _seen=" "
  # noglob for the split: the word-splitting expansion below is deliberately unquoted, so
  # without this `--subtasks '2*'` GLOBS against the cwd and any files named 20/21/22 expand
  # into the list — and then sail through the numeric check below as if they were real issue
  # numbers. Validation cannot catch that; it runs after the shell has already substituted.
  # (batch-plan.sh disables globbing script-wide for the same class of hazard.) Restored
  # immediately after, since the rest of this script relies on globbing.
  set -f
  # Split on commas AND whitespace, so `--subtasks "265, 270"` works like `265,270`.
  for _st in ${SUBTASK_ARG//,/ }; do
    # Fail LOUDLY rather than skip: a malformed entry silently dropped would lose a real
    # issue from the group, and nothing downstream would notice it was meant to ship here.
    [[ "$_st" =~ ^[0-9]+$ ]] || { set +f; wt_die "--subtasks: '$_st' is not an issue number"; }
    # `continue` on a match, but never let the FAILING test abort the loop under `set -e`:
    # a test followed by && is exempt, and an if/fi keeps that obvious rather than subtle.
    if [ "$_st" != "$ISSUE" ]; then           # the primary IS the branch, never a subtask
      case "$_seen" in
        *" $_st "*) ;;                        # dedupe: already queued
        *) _seen="${_seen}${_st} "; SUBTASK_LIST+=("$_st") ;;
      esac
    fi
  done
  set +f
  unset _seen _st
fi

# --- per-issue Model: override (issue #142) ----------------------------------
# A `Model: <id>` line in a numbered issue's body pins THIS spoke's driver model
# (same first-match, case-insensitive convention as Scope:/Gate:). An explicit
# WT_AGENT_MODEL from the environment always wins, so this only runs when unset.
if [ -z "${WT_AGENT_MODEL:-}" ] && [[ "$ISSUE" =~ ^[0-9]+$ ]] && command -v gh >/dev/null 2>&1; then
  ISSUE_BODY="$(gh issue view "$ISSUE" --json body -q .body 2>/dev/null || true)"
  MODEL_LINE="$(printf '%s\n' "$ISSUE_BODY" \
    | grep -iE '^[[:space:]]*model:[[:space:]]*[^[:space:]]' | head -1 || true)"
  if [ -n "$MODEL_LINE" ]; then
    WT_AGENT_MODEL="$(printf '%s' "$MODEL_LINE" \
      | sed -E 's/^[[:space:]]*[Mm][Oo][Dd][Ee][Ll]:[[:space:]]*//; s/[[:space:]]+$//')"
    echo "→ per-issue model  $WT_AGENT_MODEL (from issue #$ISSUE Model: line)"
  fi
fi

WT_DIR="$(dirname "$REPO_ROOT")/$(basename "$REPO_ROOT")-${WT_TAG}"

# --- create the worktree -----------------------------------------------------
git worktree prune                       # drop stale registrations first
[ -e "$WT_DIR" ] && wt_die "path already exists: $WT_DIR (open it, or remove it first)"
git fetch origin --quiet 2>/dev/null || true

if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  wt_die "branch already exists locally: ${BRANCH} — check it out, or pass a different slug"
fi
if git show-ref --verify --quiet "refs/remotes/origin/${BRANCH}"; then
  wt_warn "branch ${BRANCH} already exists on origin; the new worktree starts a fresh local branch."
fi

# Branch from the RESOLVED base (issue #117) — origin/<base> when the remote
# ref exists, else the local <base> — never implicitly from the hub's HEAD, so
# a repo integrating on develop (git config ai-toolkit.base-branch) cuts spokes
# from the right place and hub-local drift is not inherited.
BASE_BRANCH="$(wt_base_branch "$REPO_ROOT")"
BASE_START="$(wt_base_start_point "$REPO_ROOT")" \
  || wt_die "base branch '$BASE_BRANCH' has no ref (neither origin/$BASE_BRANCH nor local) — fix git config ai-toolkit.base-branch / AI_TOOLKIT_BASE_BRANCH"

echo "→ creating worktree  $WT_DIR"
echo "→ new branch         $BRANCH (from $BASE_START)"
git worktree add "$WT_DIR" -b "$BRANCH" "$BASE_START"

# Resolve the marker-emitter dir that EXISTS in the freshly-created worktree (#271): `scripts`
# in the ai-toolkit checkout (tracked, so it is checked out), `.ai-toolkit/scripts` in a synced
# target. The seed prompt + allowlist below name this path so the spoke's `bash <dir>/
# spoke-ready.sh …` marker command is runnable verbatim (and matches the deny-wall's tier-1 lane).
MARKER_DIR="$(wt_marker_script_dir "$WT_DIR")"

# --- mint the spoke_run_id ---------------------------------------------------
# Every hook/script emitting telemetry inside this worktree reads this id, so
# all spans of one spoke share it across sessions and resumes. Format:
# <branch>+<spawn-epoch>. Minting is INDEPENDENT of AI_TOOLKIT_TELEMETRY — the
# spoke's identity must exist even if telemetry is enabled later mid-run.
#
# Make .ai-toolkit/ and .claude/ ignored via the repo's git exclude (resolved
# for this worktree) rather than trusting the consuming repo's committed
# .gitignore: a synced target may ship its own .gitignore without them, and
# then the minted spoke-run-id and the seeded/copied .claude/ runtime config
# (settings.local.json below) would land UNTRACKED and break worktree-done
# (git worktree remove) and worktree-land (untracked-as-dirty guard) — masked
# on dev machines whose personal global git ignore covers .claude/ (#132).
# The exclude entries are local to the repo's git dir, never committed, and
# appended at most once each.
# The `.testmondata*` glob (issue #206) covers the testmon DB the pre-push gate
# writes AND its `-shm`/`-wal` WAL sidecars: unmanaged they read as untracked and
# the #172 ready gate refuses ready/<N> as a dirty tree. The OTel raw-request-body
# dumps live under .ai-toolkit/ (already excluded above), so no extra entry is
# needed for them.
EXCLUDE_FILE="$(git -C "$WT_DIR" rev-parse --git-path info/exclude 2>/dev/null || true)"
if [ -n "$EXCLUDE_FILE" ]; then
  mkdir -p "$(dirname "$EXCLUDE_FILE")"
  for entry in '.ai-toolkit/' '.claude/' '.testmondata*'; do
    grep -qxF "$entry" "$EXCLUDE_FILE" 2>/dev/null \
      || printf '%s\n' "$entry" >> "$EXCLUDE_FILE"
  done
fi

# --- pre-warm .testmondata from the hub baseline (issue #276) -----------------
# The first push per fresh worktree otherwise runs the FULL multi-thousand-test suite
# SOLELY to build .testmondata (12-47 min observed): test-select.sh's python tier has no
# incremental path on a worktree whose testmon DB is missing. Copy a maintained baseline
# from the hub's git-common-dir (refreshed by gate-sweep.sh after a green post-land full
# sweep) into the new worktree root, so the first push runs a testmon INCREMENTAL — only
# the branch diff's affected tests — instead of the seed. No path rewrite is needed:
# testmon stores rootdir-relative paths only, so a DB built at one absolute path is fully
# reused at another. Best-effort and guarded: a missing/unreadable baseline copies
# nothing and the first push falls back to today's full-suite seed — NEVER a wrong-green,
# because testmon's own `environment` row (system_packages + python_version) invalidates a
# copied baseline whose venv dep-set differs (a requirements-dev.txt bump re-runs full).
# The `.testmondata*` exclude seeded just above keeps the copied DB from dirtying the tree.
HUB_COMMON_DIR="$(git rev-parse --git-common-dir 2>/dev/null || true)"
case "$HUB_COMMON_DIR" in
  "")  ;;                                       # not resolvable — skip the pre-warm
  /*)  ;;                                        # already absolute
  *)   HUB_COMMON_DIR="$REPO_ROOT/$HUB_COMMON_DIR" ;;
esac
if [ -n "$HUB_COMMON_DIR" ] && [ -r "$HUB_COMMON_DIR/.testmondata-baseline" ]; then
  if cp "$HUB_COMMON_DIR/.testmondata-baseline" "$WT_DIR/.testmondata" 2>/dev/null; then
    echo "→ pre-warmed .testmondata (hub baseline; first push runs a testmon incremental)"
  else
    wt_warn "could not copy the .testmondata baseline — first push falls back to the full-suite seed"
  fi
fi

SPOKE_RUN_ID="${BRANCH}+$(date +%s)"
mkdir -p "$WT_DIR/.ai-toolkit"
printf '%s\n' "$SPOKE_RUN_ID" > "$WT_DIR/.ai-toolkit/spoke-run-id"
echo "→ spoke_run_id       $SPOKE_RUN_ID"

# Execution mode + lane pointers (#102): stamped alongside spoke-run-id so
# langfuse_spoke_tree.py can tag the reconstructed trace (groupable in Langfuse).
# `lane` is derived above (issue-backed → spoke, ad-hoc slug → express); `mode`
# is `attended` unless a supervisor (hub-afk.sh) passed `--mode afk`.
printf '%s\n' "$LANE" > "$WT_DIR/.ai-toolkit/lane"
printf '%s\n' "$MODE" > "$WT_DIR/.ai-toolkit/mode"
echo "→ lane / mode        $LANE / $MODE"

# #300 writer: record `dispatched` — the actor that CAUSES the transition (this
# spawn) records it at the instant it happens. Shadow-only: the drain still reads
# dispatch-<issue>.epoch and nothing decides on the log yet. AFK_TLOG_RUN stamps
# the freshly-minted spoke_run_id onto the record, so a spoke's whole lifecycle is
# greppable by run even across a relaunch. Best-effort (wt_tlog_* no-op without the
# lib, and skip an ad-hoc slug with no issue number).
AFK_TLOG_RUN="$SPOKE_RUN_ID" wt_tlog_transition "$ISSUE" dispatched worktree-new.sh \
  "spawn --mode $MODE" "{\"branch\":\"$BRANCH\",\"lane\":\"$LANE\",\"mode\":\"$MODE\"}"


# --- write the task contract to disk (issue #177) ----------------------------
# Anchoring used to be an LLM errand: the seed prompt told the spoke to run
# /source-task, which shells `gh issue view`. The dispatcher already knows the
# issue, so write the contract to <wt>/.ai-toolkit/task.md at spawn and point the
# seed prompts (this script's default below and hub-afk's kickoff_for) at it.
# /source-task stays for crash re-anchor (a lost task.md). Numbered issues only —
# an ad-hoc slug has no issue to fetch; best-effort, a gh miss simply leaves no
# task.md and the seed falls back to /source-task. The Scope:/Gate: control lines
# ride along inside the body verbatim.
TASK_MD="$WT_DIR/.ai-toolkit/task.md"
if [[ "$ISSUE" =~ ^[0-9]+$ ]] && command -v gh >/dev/null 2>&1; then
  # Reuse what earlier blocks already fetched so a spawn makes at most one gh call
  # per field (an unbounded gh here would double the round-trips and, under /afk's
  # synchronous dispatch, add a second hang point). TITLE comes from the slug path
  # (numeric, no-slug); ISSUE_BODY from the Model: block (when WT_AGENT_MODEL is
  # unset). Both are unset on the other paths, so `-` (not `:-`) fetches only then
  # and an already-fetched empty value is honoured, not re-fetched.
  TASK_TITLE="${TITLE-$(gh issue view "$ISSUE" --json title -q .title 2>/dev/null || true)}"
  TASK_BODY="${ISSUE_BODY-$(gh issue view "$ISSUE" --json body -q .body 2>/dev/null || true)}"
  # Write ONLY a COMPLETE contract — both title AND body present (issue #206). A
  # partial gh failure (e.g. the body fetch dies after the title fetch) that left a
  # title-only task.md would stamp a hollow contract whose mere existence suppresses
  # the seed's /source-task fallback; requiring both means such a fetch leaves no
  # task.md and the fallback still engages (same as a total gh miss). Written
  # ATOMICALLY (temp file + rename) so a mid-write interruption never leaves a
  # partial task.md that the fallback would likewise be fooled by.
  if [ -n "$TASK_TITLE" ] && [ -n "$TASK_BODY" ]; then
    TASK_TMP="$(mktemp "$WT_DIR/.ai-toolkit/task.md.XXXXXX")"
    {
      printf '# Issue #%s: %s\n\n' "$ISSUE" "$TASK_TITLE"
      printf '%s\n' "$TASK_BODY"
    } > "$TASK_TMP"
    mv "$TASK_TMP" "$TASK_MD"
    echo "→ task contract      .ai-toolkit/task.md (#$ISSUE)"

    # --- pre-seed the todo-ledger skeleton (issue #235) ----------------------
    # The step spine is script-stamped, but the ledger LABELS are still the
    # spoke's, so structure is inherited rather than invented: emit one
    # `#<issue>.<slug> · <STEP> — <label>` row (the ledger-schema-guard format)
    # per subtask x the five solo-cycle steps. Subtasks come from a "## Subtasks"
    # section of the body when present, else a single `#<issue>.main` row from the
    # title. Gitignored (.ai-toolkit/), and only written for a COMPLETE contract.
    # The `·`/`—` separators live only in printf FORMAT strings (fixed bytes, never
    # absorbed into a $var), and subtask extraction is awk-based, so nothing here
    # depends on locale-sensitive multibyte matching.
    LEDGER_SKELETON="$WT_DIR/.ai-toolkit/ledger-skeleton.md"
    SUBTASKS=$(printf '%s\n' "$TASK_BODY" | awk '
      /^[[:space:]]*#+[[:space:]]*[Ss]ubtasks/ { grab=1; next }
      grab && /^[[:space:]]*#+[[:space:]]/     { grab=0 }
      grab && /^[[:space:]]*([-*]|[0-9]+[.)])[[:space:]]+/ {
        sub(/^[[:space:]]*([-*]|[0-9]+[.)])[[:space:]]+/, "")
        print
      }')
    SINGLE=""
    if [ -z "$SUBTASKS" ]; then
      SUBTASKS="$TASK_TITLE"
      SINGLE=1
    fi
    {
      printf '# Ledger skeleton for #%s — seed your task ledger from these rows\n\n' "$ISSUE"
      while IFS= read -r label; do
        [ -n "$label" ] || continue
        if [ -n "$SINGLE" ]; then
          slug="main"
        else
          slug=$(printf '%s' "$label" | tr '[:upper:]' '[:lower:]' \
            | tr -cs 'a-z0-9' '-' | sed -e 's/^-//' -e 's/-$//' | cut -c1-24 | sed -e 's/-$//')
          [ -n "$slug" ] || slug="subtask"
        fi
        for step in ANCHOR RED GREEN REVIEW PUSH; do
          printf '#%s.%s · %s — %s\n' "$ISSUE" "$slug" "$step" "$label"
        done
        printf '\n'
      done <<EOF
$SUBTASKS
EOF
    } > "$LEDGER_SKELETON"
    echo "→ ledger skeleton    .ai-toolkit/ledger-skeleton.md"
  fi
fi

# .claude/ is gitignored runtime config (skills, hooks, settings) synced from
# shared/. `git worktree add` checks out only TRACKED files, so without this copy
# the worktree has no active skills/hooks. (`.worktreeinclude` would handle this,
# but it only runs for native `claude -w` worktrees, not `git worktree add`.)
# .review/ and *.bak are excluded — .review/ is per-checkout approval state that
# must start empty, or a push could pass on another worktree's approval.
if [ -d "$REPO_ROOT/.claude" ]; then
  echo "→ copying .claude/ runtime config (gitignored; skills + hooks + settings)"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '.review/' --exclude 'worktrees/' --exclude '*.bak' \
      "$REPO_ROOT/.claude/" "$WT_DIR/.claude/"
  else
    cp -R "$REPO_ROOT/.claude" "$WT_DIR/.claude"
    rm -rf "$WT_DIR/.claude/.review" "$WT_DIR/.claude/worktrees"
    find "$WT_DIR/.claude" -name '*.bak' -type f -delete
  fi
fi

# --- seed the spoke's command allowlist ----------------------------------------
# The spoke's PUSH step and marker emission each run as ONE allowlistable
# process — spoke-push.sh and spoke-ready.sh — because Claude Code's Bash matcher
# decomposes a compound command and requires every segment to be allowed, so a
# decorated/chained push (or the intrinsically two-command `ready/N` / `gate/N`
# marker `git tag … && git push …`) never matched a bare exact-push rule and
# always re-prompted (issues #37, #45). Seed those script rules plus a read-only
# helper allowlist (Tier 1 local, Tier 2 network-read); the ship gates
# (push-scope-guard + the pre-push hooks), not permission asks, do the enforcing.
#
# `git branch --show-current` is seeded EXACT — never `git branch:*`, which would
# hand over `git branch -D`. The runner tier (#38) is scoped to the pytest verbs
# and `chmod +x` only. The staging tier (#149) seeds `git add:*` (worktree-confined)
# and the non-destructive `git reset` unstage shapes ONLY — never the broad
# `git reset:*`, which would hand over `git reset --hard` (a working-tree wipe).
# The exec tier (#259) seeds `Bash(./:*)` so a compound self-op's `./<in-tree-script>`
# segment is honored per-segment (see the rule's own comment for the always-on trade-off).
# No directly-destructive verb is seeded: no `python:*` / `python -c:*` / `chmod:*` (bare) /
# `git tag:*` / `git push:*` / `git checkout|clean:*` / `git reset --hard` / `rm` / `mv`.
ALLOW_RULES=(
  "Bash(bash $MARKER_DIR/spoke-push.sh:*)"
  "Bash(bash $MARKER_DIR/spoke-ready.sh:*)"
  # Tier 1 — read-only, no side effects
  "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git show:*)"
  "Bash(git rev-parse:*)" "Bash(git branch --show-current)"
  "Bash(ls:*)" "Bash(cat:*)" "Bash(head:*)" "Bash(tail:*)" "Bash(wc:*)"
  "Bash(grep:*)" "Bash(rg:*)" "Bash(find:*)" "Bash(echo:*)" "Bash(tree:*)"
  # Tier 2 — network-read / read-only GitHub
  "Bash(git fetch:*)" "Bash(git remote -v)" "Bash(git stash list)"
  "Bash(gh issue view:*)" "Bash(gh pr view:*)"
  # Runner (#38) — the RED→GREEN→test loop + chmod +x on new scripts; the bare
  # python/chmod verbs stay gated (arbitrary exec / unrestricted mode bits).
  "Bash(python -m pytest:*)" "Bash(.venv/bin/python -m pytest:*)" "Bash(pytest:*)"
  "Bash(chmod +x:*)"
  # Staging (#149) — the RED-commit selective stage `git reset -q; git add <file>`
  # runs unattended without a prompt. `git add` is worktree-confined; the reset
  # shapes exclude `--hard`, so no destructive working-tree wipe is ever handed over.
  "Bash(git add:*)"
  "Bash(git reset)" "Bash(git reset -q)"
  "Bash(git reset HEAD:*)" "Bash(git reset -q HEAD:*)"
  # In-worktree self-script execution (#259). Claude Code evaluates a COMPOUND Bash command
  # PER-SEGMENT against permissions.allow (precedence: deny > ask > allow > default-prompt),
  # and a PreToolUse hook's whole-command `allow` does NOT satisfy that per-segment check. So
  # the #253 afk-permission-hook — which classifies the WHOLE command and emits `allow` for a
  # benign scoped self-op — could not suppress the dialog for the #238 smoke
  # `chmod +x X && ./X`: `chmod +x X` matched the rule above but the `./X` segment matched no
  # rule and re-prompted. Seeding `Bash(./:*)` covers that segment deterministically (the
  # in-worktree exec lane classify_permission already APPROVEs, #240 — trusting the spoke to
  # run its own in-tree scripts, no more dangerous than the seeded `pytest`).
  #
  # TRADE-OFF, on purpose: unlike the hook (self-limited to a live /afk drain), a settings
  # rule is ALWAYS-ON — it auto-runs `./…` in ATTENDED sessions in this spoke worktree too,
  # and its coarse prefix cannot express classify's worktree-confinement, so a `./../…`
  # traversal to a PRE-EXISTING out-of-tree script is NOT rejected here. What keeps that
  # residual low-risk is WRITE-CONFINEMENT: the spoke cannot CREATE an out-of-tree
  # `../evil.sh` (a write outside the worktree escalates), so `./../…` can only reach a script
  # that already exists — not an accidental-damage pattern. The deny-scope hooks remain
  # authoritative for directly-typed destructive VERBS (rm/push/chmod — which this rule never
  # grants; spoke-main-guard also catches a traversal to the land script BY NAME), and the
  # pre-push ship gates guard shared state — but none of those inspect a `./`-run script's
  # body, so write-confinement is the load-bearing guard here. An honest cost for no-dialog.
  "Bash(./:*)"
  # Hub-root READ access (#181) — a spoke routinely studies hub scripts/hooks OUTSIDE its
  # own worktree (e.g. reading the hub's .git/hooks/pre-push to understand the push cage),
  # a write-free research read that otherwise fires a permission dialog and, unattended,
  # escalates to blocked/<N>. READ-only and scoped to the hub root subtree — never Edit/Write
  # (a spoke must never mutate the hub). The `Read(//<abs>/**)` form matches Claude Code's
  # absolute-path read pattern (REPO_ROOT is already absolute, so the leading `/` yields `//`).
  "Read(/${REPO_ROOT}/**)"
)

# --- the afk deny-wall (issue #281) -------------------------------------------
# A spoke must ask in PROSE plus a gate marker, never an `AskUserQuestion`. The /afk
# injector cannot answer a QCM: inject_answer sends Escape FIRST (the #74 menu-cancel, built
# for the PLAN-gate QCM), which CANCELS a spoke's AskUserQuestion rather than selecting an
# option, then types free text at the plain prompt behind it. In #271 that left the pane
# reading "User declined to answer questions" with the options intact while the drain logged
# an injected answer -- and the free text it typed answered an EARLIER park, not the question
# actually on screen. Denying the tool removes the QCM the broker cannot drive.
#
# The rule is BARE (no parenthesised specifier) on purpose: a bare tool-name deny removes the
# tool from the model's context entirely, so the spoke never renders a QCM at all, whereas a
# scoped deny would leave the tool present and merely error on use. A deny RULE also outranks
# the permission MODE (rules > mode), so it still fires under the afk `bypassPermissions`
# launch below -- the same precedence that let a global `ask` rule pierce bypass in #238.
#
# afk-ONLY, mirroring the `--permission-mode bypassPermissions` gating below: an attended
# spoke has a human at the keyboard who can answer a QCM, and no injector Esc-cancels it.
# The spoke's own PLAN gate is unaffected in both modes -- it is TAG-based (spoke-ready.sh
# --gate writes gate/<N>, which plan-gate-guard reads), never an AskUserQuestion -- so the
# wall can never strand a spoke at its own gate.
DENY_RULES=()
[ "$MODE" = afk ] && DENY_RULES=("AskUserQuestion")

# wt_warn_deny_rules -> name the deny rules a failed merge did NOT apply. A dropped ALLOW rule
# only costs a permission prompt (loud, and the operator sees it); a dropped DENY rule silently
# removes a safety wall, so an unapplied one must never pass unremarked. No-op when there are
# none — and it guards the COUNT rather than expanding the array, which on an EMPTY array under
# `set -u` aborts on bash 3.2 (the macOS default shell).
wt_warn_deny_rules() {
  local r
  [ "${#DENY_RULES[@]}" -gt 0 ] || return 0
  wt_warn "  the afk deny-wall below is NOT applied — this spoke can still raise a QCM the drain cannot answer:"
  for r in "${DENY_RULES[@]}"; do wt_warn "  deny: $r"; done
}

SETTINGS_LOCAL="$WT_DIR/.claude/settings.local.json"
mkdir -p "$WT_DIR/.claude"
if [ ! -f "$SETTINGS_LOCAL" ]; then
  {
    printf '{\n  "permissions": {\n    "allow": [\n'
    for i in "${!ALLOW_RULES[@]}"; do
      sep=","; [ "$i" -eq "$(( ${#ALLOW_RULES[@]} - 1 ))" ] && sep=""
      printf '      "%s"%s\n' "${ALLOW_RULES[$i]}" "$sep"
    done
    # An attended spoke seeds no deny rules at all, so it keeps no empty "deny" key. Guard on
    # the COUNT and never expand the array bare: `"${DENY_RULES[@]}"` on an EMPTY array is an
    # unbound-variable abort under `set -u` on bash 3.2 (the macOS default shell).
    if [ "${#DENY_RULES[@]}" -gt 0 ]; then
      printf '    ],\n    "deny": [\n'
      for i in "${!DENY_RULES[@]}"; do
        sep=","; [ "$i" -eq "$(( ${#DENY_RULES[@]} - 1 ))" ] && sep=""
        printf '      "%s"%s\n' "${DENY_RULES[$i]}" "$sep"
      done
    fi
    printf '    ]\n  }\n}\n'
  } > "$SETTINGS_LOCAL"
  echo "→ seeded spoke command allowlist (.claude/settings.local.json)"
  if [ "${#DENY_RULES[@]}" -gt 0 ]; then echo "→ seeded afk deny-wall (AskUserQuestion)"; fi
elif command -v jq >/dev/null 2>&1; then
  # Append-without-churn: existing entries keep their order (no jq `unique`,
  # which would lexicographically re-sort a user-curated list); only rules not
  # already present are appended. A malformed file makes jq fail and a
  # zero-byte file yields zero output documents with exit 0 — in both cases
  # warn and leave the file untouched rather than abort the wiring or
  # silently truncate (-s catches the empty-output case).
  RULES_JSON="$(printf '%s\n' "${ALLOW_RULES[@]}" | jq -Rn '[inputs]')"
  # Same empty-array care as the seed path: never expand DENY_RULES bare (bash 3.2 + set -u).
  # An attended spoke merges `[]`, and the jq filter's guard then leaves the file's `deny` key
  # exactly as it found it — adding an empty `deny: []` to a user's settings would be churn.
  if [ "${#DENY_RULES[@]}" -gt 0 ]; then
    DENY_JSON="$(printf '%s\n' "${DENY_RULES[@]}" | jq -Rn '[inputs]')"
  else
    DENY_JSON='[]'
  fi
  TMP_SETTINGS="$(mktemp)"
  # Append-without-churn for BOTH lists: only rules not already present are added, so a
  # pre-existing user deny rule is preserved (dropping one would silently widen the spoke).
  if jq --argjson rules "$RULES_JSON" --argjson deny "$DENY_JSON" \
       '((.permissions.allow // []) as $cur | .permissions.allow = ($cur + ($rules - $cur)))
        | if ($deny | length) > 0
          then ((.permissions.deny // []) as $dcur | .permissions.deny = ($dcur + ($deny - $dcur)))
          else . end' \
       "$SETTINGS_LOCAL" > "$TMP_SETTINGS" 2>/dev/null && [ -s "$TMP_SETTINGS" ]; then
    mv "$TMP_SETTINGS" "$SETTINGS_LOCAL"
    echo "→ merged spoke command allowlist into settings.local.json"
  else
    rm -f "$TMP_SETTINGS"
    wt_warn "could not merge into settings.local.json (invalid JSON?) — add the allow rules yourself:"
    for r in "${ALLOW_RULES[@]}"; do wt_warn "  $r"; done
    wt_warn_deny_rules
  fi
else
  wt_warn "settings.local.json exists but jq is missing — add the allow rules yourself:"
  for r in "${ALLOW_RULES[@]}"; do wt_warn "  $r"; done
  wt_warn_deny_rules
fi

echo
echo "✓ worktree ready: $WT_DIR"
echo "  branch:         $BRANCH"

# --- 1. fold into the single VS Code review window ---------------------------
# The review window is a saved workspace file, so `add` edits its `folders`
# array directly (issue #134): `code --add` targets the *last-focused* window
# and routinely never lands the folder. VS Code hot-reloads the file. The CLI
# call survives strictly as the fallback when the file is missing or
# unparseable (wt_workspace_add returns 1 — call kept in a conditional, a bare
# call would abort this set -e script before the fallback).
if [ "$OPEN_MODE" != none ]; then
  case "$OPEN_MODE" in
    add)
      WS_FILE="$(wt_workspace_file "$REPO_ROOT")"
      if wt_workspace_add "$WS_FILE" "$WT_DIR"; then
        echo "→ added to your review workspace file: $WS_FILE (VS Code hot-reloads it)"
      elif command -v code >/dev/null 2>&1; then
        echo "→ adding to your VS Code review window (code --add)"
        code --add "$WT_DIR" \
          || wt_warn "no VS Code window to add to — open one, then run: code --add \"$WT_DIR\""
      else
        wt_warn "'code' CLI not found — in VS Code run: Shell Command: Install 'code' in PATH"
      fi
      ;;
    new-window)
      if command -v code >/dev/null 2>&1; then
        echo "→ opening a separate VS Code window"
        code "$WT_DIR"
      else
        wt_warn "'code' CLI not found — in VS Code run: Shell Command: Install 'code' in PATH"
      fi
      ;;
  esac
fi

# --- 2. spawn a terminal/tmux window for the agent ---------------------------
# Build the launch command, optionally seeded with a first prompt that claude
# receives as its initial message (e.g. "/source", or a task kickoff).
# Model+effort are pinned at dispatch time so spokes stay deterministic even
# when user-global settings change; override via WT_AGENT_MODEL / WT_AGENT_EFFORT.
# WT_SPOKE marks the session's ROLE, not its directory (issue #26): every command
# the spoke runs inherits it, so worktree-land.sh / worktree-done.sh refuse a
# spoke that cd's to the hub and tries to land or tear down its own worktree.
# Native OpenTelemetry trace export (issue #83) — strictly opt-in via
# AI_TOOLKIT_OTEL=1, a SEPARATE gate from the custom push layer's
# AI_TOOLKIT_TELEMETRY. When on, prefix the launch with Claude Code's native-OTel
# trace env so the interactive claude streams ONE nested trace per spoke, grouped
# by the spoke_run_id minted above (carried as an OTEL_RESOURCE_ATTRIBUTES key, so
# it tags every span/sub-agent/tool of the run). The secret boundary is the AUTH
# HEADER, not the endpoint: OTEL_EXPORTER_OTLP_HEADERS carries the Langfuse
# credential and is NEVER wired — it stays in the environment claude inherits, kept
# off the command line (visible in `ps`/tmux) and out of the manual-fallback advice.
# The connection ENDPOINTS are non-secret URLs, so to auto-populate Langfuse with no
# manual step they ARE wired: defaulted to the local collector when the operator
# left them unset, and an operator override is preserved verbatim (see below).
# The same treatment covers AI_TOOLKIT_OTEL_SPAN_ENDPOINT (#126): telemetry.sh's
# workflow-span fan-out (cycle step:/script/hook spans) POSTs to it over OTLP-HTTP,
# so it defaults to the collector's :4318 listener and rides the same gate.
#
# Off-box CONTENT (auto-populate): OTEL_LOG_USER_PROMPTS / OTEL_LOG_TOOL_DETAILS /
# OTEL_LOG_TOOL_CONTENT ship the user prompts and per-tool input/output off the
# machine so Langfuse renders conversation + per-tool I/O. They send content off-box,
# so they ride strictly behind this same AI_TOOLKIT_OTEL opt-in.
#
# Beyond traces, the same gate lights up two probe-proven signals (issue #88):
#   - METRICS (OTEL_METRICS_EXPORTER) — token-by-type/skill/agent + cost_usd; they
#     flush reliably and carry no content. Langfuse is NOT a metrics store, so the
#     operator routes them to a metrics sink (Prometheus/console) — see
#     dashboard/langfuse/otelcol.yaml. account_uuid is forced OFF for metrics
#     (OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false) since PII rides every datapoint.
#   - DETAILED TRACING (ENABLE_BETA_TRACING_DETAILED) — adds response.model_output
#     and system_reminders span attrs. Its destination, BETA_TRACING_ENDPOINT, is a
#     non-secret URL wired like the OTLP endpoint (defaulted, override preserved).
#     FOOTGUN: it MUST hit a different host:port than the normal OTLP endpoint, or it
#     silently kills ALL trace+log export (metrics still flow) — the defaults honour
#     the split: normal gRPC on :4317, beta HTTP on :4418.
# The normal stream exports over gRPC (OTEL_EXPORTER_OTLP_PROTOCOL=grpc): the beta
# detailed exporter is HTTP-only, so normal takes gRPC and beta takes HTTP — the
# arrangement proven to land response.model_output end-to-end in Langfuse (final
# verification pending on a live interactive spoke, free of the `-p` flush confound
# the probe ran under).
#
# Raw request bodies in FILE mode (issue #87): OTEL_LOG_RAW_API_BODIES=file:<dir>
# makes Claude Code dump each outgoing request, untruncated (no 60KB inline cap),
# to <dir>/<uuid>.request.json — the full tools array + system/messages prefix —
# so the post-run spoke-tree builder can itemize loaded context by name + exact
# size. The dir lives under the gitignored .ai-toolkit/ (bodies hold conversation
# content and stay local — only a body_ref path rides the OTLP logs signal) and is
# exported as AI_TOOLKIT_OTEL_BODY_DIR for the builder to find.
#
# OTEL_LOGS_EXPORTER=otlp is wired explicitly (not left to inheritance): the logs
# signal carries both the message bridge's source and the api_request_body log
# events whose body_refs point at the FILE-mode dumps — without it they are lost.
# DEFAULT-ON (issue: otel-default): native OTel is enabled unless the operator
# explicitly opts out with AI_TOOLKIT_OTEL=0. Setting it once here covers BOTH the
# prefix gate below AND wt_otel_bridge_preflight (worktree-lib.sh) — they read the
# same variable, and the lib is sourced into this shell, so the default propagates.
# AI_TOOLKIT_OTEL=0 is a clean, full opt-out: the prefix collapses, no body dir is
# created, and the preflight returns early (no bridge) — there is no half-on state.
#
# !!! PRIVACY — LOUD NOTE !!!  Default-on means EVERY spoke now ships CONTENT off-box
# by default: OTEL_LOG_USER_PROMPTS + OTEL_LOG_TOOL_DETAILS + OTEL_LOG_TOOL_CONTENT
# send the user's prompts and per-tool input/output to the collector/Langfuse, and
# OTEL_LOG_RAW_API_BODIES=file:<dir> dumps each FULL, untruncated outgoing request
# (system prompt + entire conversation + tools array) to disk under .ai-toolkit/.
# This is conversation content leaving the box for every run, not just metadata.
# To opt out entirely, launch the spoke with AI_TOOLKIT_OTEL=0.
# Client-side telemetry defaults from settings/ai-toolkit.yml (issue #228): sets
# AI_TOOLKIT_OTEL_DEFAULT / AI_TOOLKIT_OTEL_SPAN_ENDPOINT_DEFAULT (and the langfuse
# host/project/public-key defaults) so the toggle + endpoint below layer as
# env -> config -> hardcoded default. Best-effort; a telemetry-less config no-ops.
wt_resolve_telemetry_config "${AI_TOOLKIT_CONFIG:-$REPO_ROOT/settings/ai-toolkit.yml}"
AI_TOOLKIT_OTEL="${AI_TOOLKIT_OTEL:-${AI_TOOLKIT_OTEL_DEFAULT:-1}}"
OTEL_PREFIX=""
if [ "${AI_TOOLKIT_OTEL:-}" = "1" ]; then
  OTEL_BODY_DIR="$WT_DIR/.ai-toolkit/raw-bodies"
  mkdir -p "$OTEL_BODY_DIR"
  # The launch-prefix endpoints (normal gRPC :4317, beta HTTP :4418) are defaulted inside
  # wt_native_otel_prefix (worktree-lib.sh) — the SINGLE source shared with spoke-relaunch.sh
  # (#233) so spawn and relaunch never drift. But the span-sink endpoint must ALSO be set in
  # THIS shell: the wt_emit_lifecycle/wt_emit_script calls below run telemetry.sh's emit, whose
  # OTLP sink (telemetry.sh, gated on AI_TOOLKIT_OTEL_SPAN_ENDPOINT) fires only when it is set —
  # the helper's own defaulting happens in a command-substitution subshell and cannot leak back.
  wt_default_span_endpoint
  # repo=<name> (issue #343): the cross-project dimension, resolved from the main checkout's
  # origin so it matches the #231 land-time repo: tag; the collector lifts it onto live spans.
  OTEL_PREFIX="$(wt_native_otel_prefix "$SPOKE_RUN_ID" "$OTEL_BODY_DIR" "$(wt_repo_name "$REPO_ROOT")")"
fi

# Resolve the spoke driver's default model/effort via the shared helper (issue #142,
# #233): sync-emitted spoke-model.env -> hub config -> literal defaults. An explicit
# WT_AGENT_MODEL / WT_AGENT_EFFORT (env, or the Model: line above) always wins.
# The hub-side config path honors AI_TOOLKIT_CONFIG like sync-to-repo.sh does.
WT_CONFIG="${AI_TOOLKIT_CONFIG:-$REPO_ROOT/settings/ai-toolkit.yml}"
wt_resolve_agent_model "$SCRIPT_DIR" "$WT_CONFIG"

# Default seed prompt (issue #177): with no caller-supplied --prompt, seed the
# spoke to READ its on-disk task contract instead of anchoring via an LLM
# /source-task round-trip. An explicit --prompt (start-task, hub-afk's
# kickoff_for) still wins; ad-hoc slugs (no task.md) keep the unseeded launch.
if [ -z "$PROMPT" ] && [ -f "$TASK_MD" ]; then
  # A packed spoke (#278) owns a CHAIN, not one issue. Without this the spoke reads task.md,
  # sees a single issue, and has no idea the queue is waiting on it — so name the queued
  # issues and the order of work up front. The queue is still the mechanical authority
  # (spoke-ready.sh refuses ready/${ISSUE} while it is non-empty); this is the heads-up that
  PROMPT="Read your task contract at .ai-toolkit/task.md (issue #${ISSUE}, fetched at spawn -- no need to run /source-task). Break it into a task ledger (one entry per subtask x the solo-cycle steps ANCHOR/RED/GREEN/REVIEW/PUSH, exactly one in_progress) -- a skeleton is pre-seeded at .ai-toolkit/ledger-skeleton.md; seed your ledger from its rows so your entries match the '#<issue>.<slug> - <STEP> - <label>' schema. Honor its Gate: line: plan (the default for non-trivial work, and whenever no Gate: line is present) means the PLAN gate comes first -- explore, print the full implementation plan, emit 'bash ${MARKER_DIR}/spoke-ready.sh --gate ${ISSUE}', and WAIT for approval before GREEN; only Gate: none runs autonomous straight through. Then implement via the solo-cycle (/cycle: RED -> GREEN -> REVIEW -> PUSH). Push your own branch each subtask; when the acceptance criteria are all met, push the final subtask and emit 'bash ${MARKER_DIR}/spoke-push.sh --ready ${ISSUE}'. Do NOT self-land. If task.md is missing, or the issue was edited after spawn, run /source-task ${ISSUE} to re-anchor from the live issue."
fi

# A packed spoke (#278) owns a CHAIN, not one issue: untold, it reads task.md, sees a single
# issue, and has no idea a queue is waiting on it — then hits an unexplained ready/${ISSUE}
# refusal at the very end. So APPEND the chain note to whatever prompt is being seeded,
# INCLUDING a caller-supplied --prompt. That placement is load-bearing: every packed dispatch
# comes from hub-afk's dispatch_batch, which ALWAYS passes --prompt "$(kickoff_for ...)", so a
# note built only on the default-prompt branch above would never once reach a real packed
# spoke. Appending here also keeps --subtasks self-contained — no caller has to duplicate the
# wording for packing to work.
#
# The queue stays the mechanical authority (spoke-ready.sh refuses the terminal ready while it
# is non-empty); this is the heads-up that stops that refusal from being a surprise.
if [ "${#SUBTASK_LIST[@]}" -gt 0 ] && [ -n "$PROMPT" ]; then
  PROMPT="${PROMPT} This spoke also carries these PACKED subtask issues on this SAME branch, in order: ${SUBTASK_LIST[*]} (they share #${ISSUE}'s scope, so ONE spoke ships them all instead of each paying a full spawn + suite-seed + review + land cycle). Finish #${ISSUE}'s own work first, then for EACH queued issue run '/source-task <N>' to re-anchor, run its full solo-cycle, and emit 'bash ${MARKER_DIR}/spoke-push.sh --ready <N>' -- that clears it from the queue. Inspect the queue any time with 'bash ${MARKER_DIR}/spoke-ready.sh --queued ${ISSUE}'. Only once the queue is EMPTY do you emit the terminal 'bash ${MARKER_DIR}/spoke-push.sh --ready ${ISSUE}' -- it is REFUSED while the queue is non-empty."
fi

# afk_ask_rule_preflight -> warn (stderr, best-effort) when the user-global settings carry a
# `permissions.ask` rule that would PIERCE bypassPermissions (rules > mode, #238) and strand this
# afk spoke on a dialog. Advisory only: never blocks the spawn, and is a silent no-op when the
# settings file / python3 is absent or no ask rule exists. HONORS CLAUDE_CONFIG_DIR (the CC
# config root) before falling back to ~/.claude, matching where CC reads user settings.
afk_ask_rule_preflight() {
  local settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json" n
  [ -f "$settings" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  n="$(WT_ASK_SETTINGS="$settings" python3 - <<'PYEOF' 2>/dev/null || printf 0
import json, os
try:
    with open(os.environ["WT_ASK_SETTINGS"]) as fh:
        rules = ((json.load(fh) or {}).get("permissions") or {}).get("ask") or []
    print(len(rules) if isinstance(rules, list) else 0)
except Exception:
    print(0)
PYEOF
)"
  case "$n" in '' | 0 | *[!0-9]*) return 0 ;; esac
  printf '%s\n' "worktree-new: WARNING (afk): $settings has $n permissions.ask rule(s) -- an ask RULE pierces bypassPermissions (rules > mode) and can strand this spoke on a dialog. Remove global ask rules (or use an afk-aware machine-local hook) for a dialog-free drain." >&2
}

AGENT_CMD="${OTEL_PREFIX}WT_SPOKE=$(printf '%q' "$WT_TAG") CLAUDE_EFFORT=$(printf '%q' "$WT_AGENT_EFFORT") claude --model $(printf '%q' "$WT_AGENT_MODEL")"
# afk spokes launch under bypassPermissions (#261): the bypass MODE suppresses the routine
# prompt-then-approve dialogs, moving safety to a PreToolUse deny-hook WALL (afk-danger-guard)
# that still fires and can DENY even under bypass. But the mode is NOT absolute: a user-global
# `permissions.ask` RULE outranks the permission mode (rules > mode), so such a rule PIERCES
# bypassPermissions and still raises a dialog -- #238 proved this stranded a live afk run. The
# afk_ask_rule_preflight below warns when one exists; the operator must remove global ask rules
# (or replace them with an afk-aware machine-local hook) for a dialog-free drain. afk-ONLY:
# attended/quick lanes keep default prompting (the human is the wall), so their launch is
# unchanged. The flag precedes the seeded prompt below, which stays the trailing arg. The wall's
# own gate is .ai-toolkit/mode == afk (written above), so it stays authoritative for the whole
# bypass lifetime independent of supervisor liveness.
[ "$MODE" = afk ] && AGENT_CMD="$AGENT_CMD --permission-mode bypassPermissions"
[ "$MODE" = afk ] && afk_ask_rule_preflight
# Best-effort in-process budget cap for unattended spokes. A caller may set
# WT_AGENT_BUDGET_ARGS (e.g. "--max-budget-usd 5"); it is a pre-formed multi-arg
# string appended verbatim (NOT %q-quoted), so leave it unset for ordinary attended
# spokes to keep the launch unchanged. The supervisor-side wall-clock reap is the
# reliable ceiling; this is a backstop.
[ -n "${WT_AGENT_BUDGET_ARGS:-}" ] && AGENT_CMD="$AGENT_CMD ${WT_AGENT_BUDGET_ARGS}"
[ -n "$PROMPT" ] && AGENT_CMD="$AGENT_CMD $(printf '%q' "$PROMPT")"

# --- seed the queued-subtask channel (issue #278) ----------------------------
# The packed group's extra issues become this spoke's subtask queue. Seeded HERE, at spawn,
# rather than only on hub-afk's routing pass: /next-batch dispatches interactively with no
# drain running, so a packed spoke would otherwise find an empty queue, emit ready/<primary>,
# and silently drop its subtasks on the floor.
#
# Placed THIS LATE on purpose — after every fallible setup step (task.md, the allowlist
# merge, model resolution, the gh label mirror), immediately before the launch. This dir is
# keyed by ISSUE and SHARED, not worktree-local, so a spawn that dies after seeding would
# strand it: a later, unrelated spoke for the same issue would inherit the entries and be
# refused at ready/<primary> forever. Nothing before this point can now leave that behind.
#
# The path contract (<git-common-dir>/ai-toolkit-afk/queued-<spoke>/<issue>, one empty file
# per queued issue) is INLINED rather than sourced: its owner, gate-broker-markers.sh, is a
# hub-skill module this script cannot reach from a synced target — the same split the
# outbound event spool already lives with, where the two sides share only the path. One file
# per issue keeps create/unlink atomic, so this can never race the spoke's own clears.
# Best-effort: the queue is a scheduling optimization and must not fail an otherwise-fine
# spawn (the spoke would simply ship its primary and the subtasks dispatch fresh).
if [ "${#SUBTASK_LIST[@]}" -gt 0 ]; then
  # --git-common-dir answers RELATIVE to cwd (a bare `.git` from the checkout root), so
  # anchor it on $REPO_ROOT rather than trusting wherever this script happens to stand.
  _q_common="$(cd "$REPO_ROOT" && git rev-parse --git-common-dir 2>/dev/null || printf '.git')"
  case "$_q_common" in /*) ;; *) _q_common="$REPO_ROOT/$_q_common" ;; esac
  _q_dir="${AFK_STATE_DIR:-$_q_common/ai-toolkit-afk}/queued-$ISSUE"
  mkdir -p "$_q_dir" 2>/dev/null || true
  for _st in "${SUBTASK_LIST[@]}"; do
    : > "$_q_dir/$_st" 2>/dev/null || true
  done
  echo "→ queued subtasks    ${SUBTASK_LIST[*]} (shipped on this branch before ready/$ISSUE)"
  unset _q_common _q_dir _st
fi

# Bring up the otelcol collector, then the Langfuse message bridge, before the
# spoke starts streaming, so an opted-in (AI_TOOLKIT_OTEL=1) spoke auto-populates
# Langfuse with no manual step. Order matters: the collector (:4317, what CC
# exports to) forks to the bridge (:4319), so it must be up first. Both are
# idempotent (never a second instance) and best-effort (warn, never fail the spawn).
wt_otel_collector_preflight "$REPO_ROOT"
wt_otel_bridge_preflight "$REPO_ROOT"

if [ "$SPAWN_TERMINAL" -eq 1 ]; then
  SPAWNED=0
  if command -v tmux >/dev/null 2>&1; then
    win_name="${BRANCH##*/}"
    # one tmux session per project (issue #39): derive it from the repo root so
    # spokes nest under their project and 'tmux ls' reads as a portfolio.
    sess="$(wt_tmux_session "$REPO_ROOT")"
    # ensure the project session exists, detached if need be; '=' pins the
    # target to an exact session name so e.g. '<sess>-foo' can never match
    if tmux has-session -t "=$sess" 2>/dev/null || tmux new-session -d -s "$sess" -c "$REPO_ROOT" 2>/dev/null; then
      # The launch command is the window's own shell command, not keystrokes:
      # typing it via send-keys raced interactive-zsh init (eaten Enter, zvm) —
      # issue #15. `exec $SHELL` keeps the window alive after claude exits.
      if [ "$LAUNCH_AGENT" -eq 1 ]; then
        win="$(tmux new-window -t "=$sess:" -P -F '#{window_id}' -n "$win_name" -c "$WT_DIR" \
               "$AGENT_CMD; exec ${SHELL:-zsh}")"
      else
        win="$(tmux new-window -t "=$sess:" -P -F '#{window_id}' -n "$win_name" -c "$WT_DIR")"
      fi
      # pin name so the running process can't clobber it
      tmux set-window-option -t "$win" automatic-rename off
      tmux set-window-option -t "$win" allow-rename off
      echo "→ opened tmux window '$win_name' ($win) in session $sess"
      if [ "$LAUNCH_AGENT" -eq 1 ]; then
        [ -n "$PROMPT" ] && echo "  launched: claude (seeded with first prompt)" || echo "  launched: claude"
      fi
      # print the exact jump command so the caller can copy-paste
      if [ -n "${TMUX:-}" ]; then
        echo "  tmux switch-client -t '${sess}:${win_name}'"
      else
        echo "  tmux attach -t '${sess}' \\; select-window -t '${sess}:${win_name}'"
      fi
      SPAWNED=1
    fi
  fi
  if [ "$SPAWNED" -eq 0 ]; then
    echo
    echo "  Start the agent in a new terminal window:"
    [ "$LAUNCH_AGENT" -eq 1 ] && echo "    cd \"$WT_DIR\" && $AGENT_CMD" || echo "    cd \"$WT_DIR\""
  fi
fi

# The one-shot preflights above only cover the spawn instant; the watchdog
# daemon keeps the collector+bridge alive for the whole spoke lifetime (machine
# sleep/wake, #138) and exits itself when the last spoke pane closes. Armed
# AFTER the tmux spawn so its first tick can already see the new pane;
# best-effort and self-gating (no-op unless AI_TOOLKIT_OTEL=1, singleton).
wt_otel_watch_arm "$REPO_ROOT"

# --- GitHub lifecycle-label mirror: dispatch (issue #236) --------------------
# Stamp the issue so its GitHub list entry shows the spoke is live: status:in-progress
# + mode:<attended|afk> + lane:spoke, plus a one-time dispatch comment linking the
# issue back to the branch / worktree / tmux window / spoke_run_id (and thus its
# Langfuse session). Numbered issues only — an ad-hoc slug carries no issue, so the
# express/quick/micro lanes mirror nothing by construction. Every write is
# best-effort and time-bounded inside the wt_gh_* helpers, so a failed / hung /
# absent / opted-out gh never fails the spawn. LANE is "spoke" for numbered issues
# (derived above). The tmux window (if one was spawned) links the live pane.
# UPGRADE: correcting the mode label of an ALREADY-attended spoke when a drain
# arms mid-run is left to a follow-up — dispatch stamps mode once, and hub-afk
# passes --mode afk for drain-dispatched spokes so those are correct at spawn.
if [[ "$ISSUE" =~ ^[0-9]+$ ]]; then
  # Name the tmux window ONLY when one was actually spawned (SPAWNED=1): a tmux
  # present-but-failed spawn still leaves win_name/sess assigned, so gate on SPAWNED
  # to avoid naming a window that doesn't exist.
  if [ "${SPAWNED:-0}" -eq 1 ] && [ -n "${win_name:-}" ]; then
    DISPATCH_WINDOW="${sess:-}:${win_name}"
  else
    DISPATCH_WINDOW="(no tmux window)"
  fi
  wt_gh_apply_dispatch_labels "$ISSUE" "$MODE" "$LANE"
  wt_gh_dispatch_comment "$ISSUE" "$(printf 'Dispatched — spoke is live (issue #236 lifecycle mirror).\n- branch: %s\n- worktree: %s\n- tmux window: %s\n- spoke_run_id: %s' \
    "$BRANCH" "$WT_DIR" "$DISPATCH_WINDOW" "$SPOKE_RUN_ID")"
fi

# --- telemetry: spawn lifecycle marker + script run-node ---------------------
# Attributed to the new spoke (emitted with the worktree as CWD), carrying the
# spoke_run_id minted above. The script span is this control script as a trace
# node; it shares its name with the lifecycle marker (emission-link basis). No-op
# unless AI_TOOLKIT_TELEMETRY=1.
wt_emit_lifecycle "worktree-new" "spawn" "success" "$WT_T0" "$WT_DIR"
wt_emit_script "worktree-new" "success" "$WT_T0" "$WT_DIR"

echo
if [ -f "$TASK_MD" ]; then
  echo "  Task contract on disk:  .ai-toolkit/task.md  (the spoke reads it, then /cycle)"
  echo "  Crash re-anchor:        /source-task $ISSUE"
else
  echo "  Then in that session, run:  /source-task   (anchor to the issue, then /cycle)"
fi
