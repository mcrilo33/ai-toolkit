#!/usr/bin/env bash
# gate-broker-classify.sh -- split out of gate-broker.sh (issue #275).
#
# A pure function-definition module of the gate-broker core. Sourced by the entry lib
# gate-broker.sh AFTER worktree-lib/hub-inject/log/afk_now and BEFORE any function is
# called, so every cross-module helper resolves at call time. Not run on its own.
set -uo pipefail

# --- the permission classifier (issue #149) -----------------------------------
# A spoke under /afk stalls on Claude Code PERMISSION dialogs (distinct from the
# question/gate parks the answerer handles): the FIRST RED-commit selective stage
# `git reset -q; git add <own file>` prompts and, unanswered, the spoke idles until
# reaped. classify_permission decides such a dialog the way a human would — but by a
# fixed rules table, not the reasoning answerer, since the decision is mechanical and
# must be conservative. It is the unit-tested heart of the supervisor's permission
# handling (the tmux detection + injection that drives it lives in decide_and_act).

# _pytest_seg_scoped <segment> -> rc 0 when a `pytest` / `python -m pytest` segment carries a
# genuine SCOPING argument (a path or node-id), rc 1 otherwise. A bare `pytest`, one carrying
# only flags (`pytest -q`, `pytest -x`), OR one whose only non-flag token is a value belonging
# to a selection option (`pytest -k foo`, `pytest -m slow`, `pytest -p plugin`) still collects
# the WHOLE suite, whose escaped tests rewrite real refs (#135) — the full-suite ref-rewind
# hazard (#203). A separate-token value of such an option is therefore SKIPPED, not counted as
# a path. Tokens are walked by hand (no word-splitting) so a glob argument never expands.
_pytest_seg_scoped() {
  local seg="$1" rest tok skip_val=0
  case "$seg" in
    'python -m pytest'*)          rest="${seg#python -m pytest}" ;;
    'python3 -m pytest'*)         rest="${seg#python3 -m pytest}" ;;
    '.venv/bin/python -m pytest'*) rest="${seg#.venv/bin/python -m pytest}" ;;
    'pytest'*)                    rest="${seg#pytest}" ;;
    *) return 1 ;;
  esac
  while [ -n "$rest" ]; do
    rest="${rest#"${rest%%[![:space:]]*}"}"          # ltrim
    [ -n "$rest" ] || break
    tok="${rest%%[[:space:]]*}"                       # first token
    rest="${rest#"$tok"}"
    if [ "$skip_val" -eq 1 ]; then skip_val=0; continue; fi   # a prior option's value token
    case "$tok" in
      # separate-token value options: the NEXT token is a value, not a scoping path.
      -k | -m | -p | -c | -o | -W | -n | -r | --rootdir | --deselect | --ignore \
        | --ignore-glob | --confcutdir | --override-ini) skip_val=1 ;;
      -*) ;;                                          # any other flag (incl. --opt=value)
      *) return 0 ;;                                  # a genuine non-flag token = a path/node-id
    esac
  done
  return 1
}

# --- benign in-worktree mutation lane (issue #203, finding 4) ------------------
# A confirmation dialog on a COMPOUND command (cd into the worktree, mv a stashed file from
# the scratchpad, chmod +x it, stash pop, targeted pytest) used to classify as one opaque
# "risky" string and escalate, wedging the whole drain. These helpers let classify_permission
# APPROVE segments whose writes are confined to the spoke's OWN worktree or its session
# scratchpad — the spoke already has unrestricted Edit/Write there, so a chmod on its own new
# hook script carries no additional risk. .git/ internals and secret-like paths stay denied.

# _broker_path_physically_in <abs> <wt> <tasks> -> rc 0 when <abs>, with ALL symlinks
# resolved, is physically under the worktree or the tasks root and NOT under <wt>/.git; rc 1
# otherwise. Closes the symlink-indirection escape a textual check cannot see: a logically
# in-tree path (e.g. `.venv/bin/python3`, a symlink worktree-new.sh points out of tree) can
# physically resolve anywhere. os.path.realpath resolves the existing prefix — following a
# final symlink FILE (the overwrite case) — and appends any not-yet-created tail, so it works
# for create targets too. Fails CLOSED (rc 1) without python3: an unverifiable mutation path
# is denied, not trusted (a false deny escalates — the safe direction).
_broker_path_physically_in() {
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_ABS="$1" _AFK_WT="$2" _AFK_TASKS="$3" python3 2>/dev/null <<'PYEOF'
import os, sys

abs_ = os.path.realpath(os.environ["_AFK_ABS"])
wt = os.path.realpath(os.environ["_AFK_WT"])
tasks = os.path.realpath(os.environ["_AFK_TASKS"])

def under(p, root):
    return p == root or p.startswith(root.rstrip("/") + "/")

if not (under(abs_, wt) or under(abs_, tasks)):
    sys.exit(1)
# Reject any `.git` path component, case-INSENSITIVELY: macOS's default filesystem is
# case-insensitive, so `.GIT` addresses the same dir as `.git` and a literal-`.git` guard
# alone misses it; this also covers a nested repo's `.git` anywhere under the roots.
if any(part.lower() == ".git" for part in abs_.split(os.sep)):
    sys.exit(1)
sys.exit(0)
PYEOF
}

# _broker_resolve_in_roots <path> <cwd> <wt> <slug> <tasks> -> print <path>'s absolute form
# (resolved against <cwd>) IF it lies under the worktree <wt> or the spoke's session
# scratchpad (<tasks>/claude-*/<slug>/…), and NOT under <wt>/.git; else rc 1. TWO layers:
# a textual containment check (fast, and the only one that can bound the scratchpad glob),
# THEN a physical symlink-resolving check (_broker_path_physically_in) — both must pass.
# Any token the shell would EXPAND to a different path (traversal, variable/command
# substitution, tilde, brace or glob metacharacters) is rejected outright: a textual
# resolver cannot see through those, and a false deny escalates — the safe direction.
_broker_resolve_in_roots() {
  local p="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" abs
  # Reject any token the shell rewrites at execution to a path the textual/realpath checks
  # cannot see: traversal (`..`), variable/command substitution (`$`, backtick), tilde, brace
  # and glob metacharacters, quoting/escaping (`"` `'` `\`), and redirection (`>` `<`). Two
  # are load-bearing beyond the obvious: a leading quote/backslash (`rm "/etc/x"`) makes the
  # `/*` absolute test below miss it so it is joined onto the worktree cwd as if relative, and
  # a redirection (`cd foo>/etc/x`) hides an out-of-tree target the shell splits off — this
  # resolver is the cd-handler's ONLY guard, so it must reject `>`/`<` that _permission_seg_safe
  # rejects on the mutation path. realpath treats all these as ordinary chars, so an escaped
  # target would pass containment yet the shell mutates the real path. A false deny escalates.
  case "$p" in
    *'..'* | *'$'* | *'`'* | '~'* | *'{'* | *'}'* | *'*'* | *'?'* | *'['* | *']'* \
      | *'"'* | *"'"* | *'\'* | *'>'* | *'<'*) return 1 ;;
  esac
  case "$p" in /*) abs="$p" ;; *) abs="$cwd/$p" ;; esac
  # Collapse `/./` and duplicate slashes textually (no glob, no fs touch). The replacement
  # is `$sl` (a bare slash held in a var), NOT a literal `\/`: bash keeps the backslash in a
  # `${var//pat/repl}` replacement string, so `\/` would corrupt the path (`/x/./y`→`/x\/y`).
  local sl=/
  while case "$abs" in */./* | *//*) true ;; *) false ;; esac; do
    abs="${abs//\/.\//$sl}"; abs="${abs//\/\//$sl}"
  done
  abs="${abs%/.}"                                  # a trailing `/.` (bare `.` target) → the dir
  abs="${abs%/}"; [ -n "$abs" ] || abs="/"
  case "$abs" in "$wt"/.git | "$wt"/.git/*) return 1 ;; esac      # never .git internals (textual)
  case "$abs" in
    "$wt" | "$wt"/*) ;;                                           # under the worktree
    "$tasks"/claude-*/"$slug"/*) ;;                               # under the scratchpad
    *) return 1 ;;
  esac
  _broker_path_physically_in "$abs" "$wt" "$tasks" || return 1   # symlink-resolved containment
  printf '%s\n' "$abs"
}

# _broker_seg_secretlike <token> -> rc 0 when a path token looks like a secret (a mutation of
# it is never in the benign lane, even inside the worktree). Mirrors the repo's own secret
# .gitignore classes (.env, *.pem) plus the common credential filenames. Matched case-
# INSENSITIVELY (via tr — bash 3.2 lacks `${v,,}`): macOS's default filesystem is case-
# insensitive, so `.ENV` addresses the same inode as `.env` and must not slip the guard
# (mirroring the case-folded `.git` component check in _broker_path_physically_in).
_broker_seg_secretlike() {
  local base lower path_lower
  base="${1##*/}"
  lower="$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')"
  case "$lower" in
    .env | .env.* | *.pem | *.key | *.p12 | id_rsa | id_dsa | id_ecdsa | id_ed25519 \
      | .netrc | credentials | .npmrc | .pypirc) return 0 ;;
  esac
  path_lower="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  # Credential STORES by directory (issue #261 review): the SSH/AWS/GPG dirs plus the common
  # cloud/registry credential dirs, so a `cat ~/.kube/config` / `~/.docker/config.json` /
  # gcloud / gh-token read is recognized as secret-like and the deny-wall's credential lane
  # stops it. UPGRADE: a novel cred store not listed here falls to the Tier-3 judge, not a
  # static deny -- extend this set as the #261 journal surfaces new ones.
  case "$path_lower" in
    */.ssh/* | */.aws/* | */.gnupg/* | */.kube/* | */.docker/* \
      | */.config/gcloud/* | */.config/gh/*) return 0 ;;
  esac
  return 1
}

# _permission_seg_mutation_ok <segment> <cwd> <wt> <slug> <tasks> -> rc 0 when a mutating
# segment (mv/cp/rm/mkdir/chmod) touches ONLY paths under the worktree or the spoke's
# scratchpad, none secret-like, none the worktree root itself. Tokens are walked by hand (no
# word-splitting) so a glob argument never expands. Inert (rc 1) without a worktree context.
_permission_seg_mutation_ok() {
  local seg="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" verb rest tok resolved saw_path=0 mode_pending=0
  [ -n "$wt" ] || return 1
  verb="${seg%% *}"
  case "$verb" in
    mv | cp | rm | mkdir | chmod) ;;
    *) return 1 ;;
  esac
  [ "$verb" = chmod ] && mode_pending=1        # chmod's first non-flag token is the mode
  rest="${seg#"$verb"}"
  while [ -n "$rest" ]; do
    rest="${rest#"${rest%%[![:space:]]*}"}"    # ltrim
    [ -n "$rest" ] || break
    tok="${rest%%[[:space:]]*}"                 # first token
    rest="${rest#"$tok"}"
    # `-t DIR` / `-tDIR` / `--target-directory[=DIR]` (GNU mv/cp) hide the DESTINATION inside
    # a flag; the glued/`=`-form would be skipped as a flag and its out-of-tree target never
    # checked. Deny the whole segment when one appears — a false deny escalates (BSD mv/cp on
    # the macOS host lacks -t, but this repo also runs on Linux/GNU coreutils).
    case "$tok" in
      -t | -t?* | --target-directory | --target-directory=*) return 1 ;;
    esac
    case "$tok" in -*) continue ;; esac         # a flag (mv -f, mkdir -p, …)
    if [ "$mode_pending" -eq 1 ]; then mode_pending=0; continue; fi
    _broker_seg_secretlike "$tok" && return 1
    resolved="$(_broker_resolve_in_roots "$tok" "$cwd" "$wt" "$slug" "$tasks")" || return 1
    [ "$resolved" = "$wt" ] && return 1         # never target the worktree root itself
    saw_path=1
  done
  [ "$saw_path" -eq 1 ]
}

# _permission_seg_exec_ok <segment> <cwd> <wt> <slug> <tasks> -> rc 0 when the segment EXECUTES
# a spoke-authored in-tree script via a `./<relative-path>` invocation whose executable resolves
# under the worktree or the spoke's session scratchpad (via _broker_resolve_in_roots — the same
# scope the mutation lane uses — which rejects `..`, absolute paths, `.git`, and shell
# metacharacters). Trailing args are opaque to WHICH code runs and are left to the
# script; the segment-level substitution/redirection reject in _permission_seg_safe has already
# fired before this is reached. Inert (rc 1) without a worktree context. Approving this is a
# worktree-trust-boundary call (#240): the gate protects SHARED state — main, the remote, sibling
# worktrees, out-of-tree paths — and trusts the spoke inside its OWN worktree, where it already
# has auto-accepted edits and where an APPROVEd targeted pytest already runs spoke-authored code.
_permission_seg_exec_ok() {
  local seg="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" tok resolved
  [ -n "$wt" ] || return 1
  tok="${seg%%[[:space:]]*}"                   # the executable (first token)
  case "$tok" in './'*) ;; *) return 1 ;; esac # only the relative ./ self-op form
  _broker_seg_secretlike "$tok" && return 1
  resolved="$(_broker_resolve_in_roots "$tok" "$cwd" "$wt" "$slug" "$tasks")" || return 1
  [ "$resolved" = "$wt" ] && return 1          # never "execute" the worktree root itself
  return 0
}

# _permission_seg_marker_ok <segment> <cwd> <wt> <slug> <tasks> -> rc 0 when the segment EMITS a
# workflow marker via the canonical emitter — `bash <path>/spoke-ready.sh …` or `bash
# <path>/spoke-push.sh …` — with the script resolving inside the spoke's worktree/scratchpad and
# every argument fitting the marker shape: a state flag (--gate|--accept|--blocked|--ready), a
# numeric issue, an optional --plan-file whose path ALSO resolves in-tree (so an out-of-tree file
# is never read into the pushed tag body), and an optional -m/--message reason (free-form — once
# seen the segment tail is accepted, since the segment-level substitution/redirection reject in
# _permission_seg_safe has already fired). Gate-marker emission is the drain's MOST critical
# control-plane op (#271): a deterministic Tier-1 lane keeps it off the probabilistic Tier-3 judge,
# which was denying `bash <path>/spoke-ready.sh --gate <N>` and leaving a spoke unable to park. The
# `./`-invoked form is already the #240 exec lane's job, so this lane only adds the `bash <path>`
# invocation (worktree-new.sh's seed prompt, hub-afk.sh's nudge, solo-cycle all use it). Inert
# (rc 1) without a worktree context — the SAME confinement discipline as the #203/#240 lanes,
# reusing _broker_resolve_in_roots (which rejects `..`, absolute paths, `.git`, secret-like names,
# and shell metacharacters).
_permission_seg_marker_ok() {
  local seg="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" rest tok script val_pending=""
  [ -n "$wt" ] || return 1
  case "$seg" in
    'bash '*) rest="${seg#bash }" ;;             # only the canonical `bash <path>` invocation
    *) return 1 ;;
  esac
  rest="${rest#"${rest%%[![:space:]]*}"}"        # ltrim
  script="${rest%%[[:space:]]*}"                 # the script path (first token)
  rest="${rest#"$script"}"
  # The basename must be EXACTLY one of the two canonical emitters — never a same-named decoy
  # elsewhere and never an arbitrary script.
  case "${script##*/}" in
    spoke-ready.sh | spoke-push.sh) ;;
    *) return 1 ;;
  esac
  _broker_seg_secretlike "$script" && return 1
  _broker_resolve_in_roots "$script" "$cwd" "$wt" "$slug" "$tasks" >/dev/null || return 1
  # Walk the arguments by hand (no word-splitting) so a glob never expands; each must fit the
  # marker shape or the whole segment is denied (default-deny).
  while [ -n "$rest" ]; do
    rest="${rest#"${rest%%[![:space:]]*}"}"      # ltrim
    [ -n "$rest" ] || break
    tok="${rest%%[[:space:]]*}"                   # first token
    rest="${rest#"$tok"}"
    if [ "$val_pending" = plan ]; then           # a separate-token --plan-file value
      # In-tree AND not secret-like: spoke-ready.sh reads --plan-file into the pushed tag body,
      # so a `--plan-file .env` would leak an in-tree secret to the remote (same guard the
      # mutation/exec lanes apply to their path tokens).
      _broker_seg_secretlike "$tok" && return 1
      _broker_resolve_in_roots "$tok" "$cwd" "$wt" "$slug" "$tasks" >/dev/null || return 1
      val_pending=""; continue
    fi
    case "$tok" in
      # A -m/--message reason is free-form: accept the remainder (the segment-level metachar
      # reject already fired). spoke-ready.sh rejects -m together with --plan-file, so a
      # --plan-file smuggled AFTER -m never reaches its file read (usage error) — but that
      # exclusion lives downstream; do not add a --plan-file after -m expecting this lane to vet it.
      -m | --message | --message=*) return 0 ;;
      --plan-file) val_pending=plan ;;
      --plan-file=*)
        _broker_seg_secretlike "${tok#--plan-file=}" && return 1
        _broker_resolve_in_roots "${tok#--plan-file=}" "$cwd" "$wt" "$slug" "$tasks" \
          >/dev/null || return 1 ;;
      --gate | --accept | --blocked | --ready | -h | --help) ;;
      --ready=*) case "${tok#--ready=}" in '' | *[!0-9]*) return 1 ;; esac ;;
      -*) return 1 ;;                             # any other flag is not marker shape
      *) case "$tok" in '' | *[!0-9]*) return 1 ;; esac ;;   # a bare positional must be the numeric issue
    esac
  done
  [ -z "$val_pending" ]                           # a dangling --plan-file (no value) is malformed
}

# _permission_seg_safe <segment> [cwd wt slug tasks] -> true when ONE command segment is a
# safe scoped self-op the spoke legitimately runs on its OWN worktree: the same vetted class
# worktree-new.sh seeds into the spoke allowlist (unstage/stage, own-file pytest,
# read-only helpers). A segment carrying command substitution, backticks, or a
# redirection is never safe — those could smuggle a destructive op behind a safe
# prefix. `git reset`'s working-tree-mutating modes (`--hard`/`--merge`/`--keep`) are
# rejected before the safe `git reset` prefix matches — only unstage/uncommit is safe.
# Everything unrecognised is unsafe (default-deny).
_permission_seg_safe() {
  local seg="$1" cwd="${2:-}" wt="${3:-}" slug="${4:-}" tasks="${5:-}"
  case "$seg" in
    *'$('* | *'`'* | *'>'* | *'<'*) return 1 ;;   # substitution / redirection smuggling
  esac
  # Benign in-worktree mutation lane (#203): when we know the spoke's worktree, a mutating
  # verb (mv/cp/rm/mkdir/chmod) is decided ENTIRELY by the lane — approve when confined to
  # the worktree or its scratchpad, else deny. Deciding it here (not falling through) is what
  # keeps the legacy relative-only `chmod +x` rule below from re-approving a lane MISS such as
  # `chmod +x .git/hooks/pre-commit`. Without worktree context the lane is inert and these
  # verbs fall through to the context-free rules (the relative-only chmod rule / default-deny).
  case "$seg" in
    'mv '* | 'cp '* | 'rm '* | 'mkdir '* | 'chmod '*)
      if [ -n "$wt" ]; then
        _permission_seg_mutation_ok "$seg" "$cwd" "$wt" "$slug" "$tasks" && return 0
        return 1
      fi ;;
  esac
  # Benign in-worktree EXECUTION lane (#240): running the spoke's OWN in-tree script
  # (`./path/to/script.sh`) is a scoped self-op, decided ENTIRELY by the lane when a worktree
  # is known — mirroring the mutation lane above so the context-free rules below never re-judge
  # it. Without a worktree context the lane is inert and `./…` falls through to default-deny.
  case "$seg" in
    './'*)
      if [ -n "$wt" ]; then
        _permission_seg_exec_ok "$seg" "$cwd" "$wt" "$slug" "$tasks" && return 0
        return 1
      fi ;;
  esac
  # Marker-emission lane (#271): the drain's most critical control-plane op — emitting a workflow
  # marker via `bash <path>/spoke-{ready,push}.sh …` — is decided ENTIRELY by the lane when a
  # worktree is known (a deterministic Tier-1 APPROVE, never the Tier-3 judge), else `bash …`
  # falls through to default-deny. Same shape as the mutation/exec lanes above.
  case "$seg" in
    'bash '*)
      if [ -n "$wt" ]; then
        _permission_seg_marker_ok "$seg" "$cwd" "$wt" "$slug" "$tasks" && return 0
        return 1
      fi ;;
  esac
  case "$seg" in
    *'--hard'* | *'--merge'* | *'--keep'*) return 1 ;;  # reset modes that touch the worktree
    'git reset' | 'git reset '* ) return 0 ;;      # unstage/uncommit only — worktree-local
    'git add' | 'git add '* ) return 0 ;;          # stage — worktree-confined
    'git status' | 'git status '* | 'git diff' | 'git diff '* ) return 0 ;;
    'git log' | 'git log '* | 'git show' | 'git show '* ) return 0 ;;
    'git rev-parse' | 'git rev-parse '* | 'git branch --show-current' ) return 0 ;;
    'git fetch' | 'git fetch '* ) return 0 ;;
    # git stash is worktree/stash-local (never touches main or the remote): pop/apply restore
    # the spoke's own stashed work, push/save stash it, list/show inspect it (#203 finding 4).
    'git stash' | 'git stash pop'* | 'git stash apply'* | 'git stash push'* \
      | 'git stash save'* | 'git stash list'* | 'git stash show'* ) return 0 ;;
    # pytest MUST carry a NON-FLAG argument (a path / node-id): a bare `pytest` OR one
    # carrying only flags (`pytest -q`, `pytest -x`) still runs the whole suite, whose
    # escaped tests rewrite real refs (#135) — the full-suite ref-rewind hazard. Requiring
    # a token (not merely any token) closes the flag-only bypass (#203).
    'pytest '* | 'python -m pytest '* | 'python3 -m pytest '* | '.venv/bin/python -m pytest '* )
      _pytest_seg_scoped "$seg" && return 0 || return 1 ;;
    'ls' | 'ls '* | 'cat '* | 'head '* | 'tail '* | 'wc' | 'wc '* ) return 0 ;;
    'grep '* | 'rg '* | 'echo' | 'echo '* | 'tree' | 'tree '* ) return 0 ;;
    'find '* )
      # A read-only find is a fine self-op, but any side-effecting primary is not: `-delete`
      # destroys files; `-exec`/`-execdir`/`-ok`/`-okdir` spawn processes; `-fprint`/`-fprintf`/
      # `-fprint0`/`-fls` write to an arbitrary file. Deny them all (#171 + review). `-print`/
      # `-printf` write only to stdout and stay allowed. Over-denial (a filename that happens to
      # contain one of these) escalates to a human, the safe direction for a default-deny guard.
      case "$seg" in *-delete* | *-exec* | *-ok* | *-fprint* | *-fls* ) return 1 ;; esac
      return 0 ;;
    'chmod +x '* )
      # chmod +x only on a RELATIVE, in-tree path. Reject an absolute target (a leading `/` or
      # a later ` /` token like `chmod +x a /bin/x`) and any `..` that would traverse out of the
      # spoke's worktree (#171 + review). A false deny (a filename containing `..`) escalates.
      case "$seg" in *' /'* | 'chmod +x /'* | *'..'* ) return 1 ;; esac
      return 0 ;;
    * ) return 1 ;;
  esac
}

# --- read-only Read tool lane (issue #181) ------------------------------------
# A spoke parks on a `Read` PERMISSION dialog for a legitimate, write-free research read —
# a hub script/hook (#175 parked on Read(<hub>/.git/hooks/pre-push)) or a sibling worktree.
# extract_pending_command surfaces such a park as "Read <file_path>"; classify_permission
# AUTO-APPROVES it when the target is confined to the repo family (the main root + its
# worktrees) and is not secret-like. A Read mutates nothing, so — unlike the write lane above
# — .git internals are readable; only the global secret classes (~/.ssh, ~/.aws, *.pem,
# id_rsa*, credential confs) stay denied. Every OTHER non-Bash tool arrives as a bare name and
# keeps default-deny.

# _broker_repo_family_roots <wt> -> print each repo-family root (the main worktree PLUS every
# linked worktree, from `git worktree list`), realpath-canonicalized, one per line. Empty when
# <wt> is not a git worktree. This is the read scope a spoke legitimately studies.
_broker_repo_family_roots() {
  local wt="$1" line p
  [ -n "$wt" ] || return 0
  git -C "$wt" worktree list --porcelain 2>/dev/null | while IFS= read -r line; do
    case "$line" in
      'worktree '*) p="${line#worktree }"; wt_realpath "$p" ;;
    esac
  done
}

# _broker_read_in_family <path> <wt> -> print <path>'s realpath (symlinks followed) IF it
# resolves under some repo-family root, else rc 1. Resolves <path> against the worktree cwd when
# relative, mirroring _broker_path_physically_in. The printed realpath lets the caller re-check
# the secret class on the RESOLVED surface (a benign-named in-family symlink to a key, or a
# trailing-slash form, evades a raw-path-only check). Fails CLOSED (rc 1) without python3 or a
# resolvable family — an unverifiable read escalates, the safe direction.
_broker_read_in_family() {
  local path="$1" wt="${2:-}" roots
  roots="$(_broker_repo_family_roots "$wt")"
  [ -n "$roots" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  _AFK_PATH="$path" _AFK_WT="$wt" _AFK_ROOTS="$roots" python3 2>/dev/null <<'PYEOF'
import os, sys

path = os.environ["_AFK_PATH"]
wt = os.environ.get("_AFK_WT", "")
if not os.path.isabs(path) and wt:
    path = os.path.join(wt, path)
abs_ = os.path.realpath(path)

def under(p, root):
    return p == root or p.startswith(root.rstrip("/") + "/")

for root in os.environ["_AFK_ROOTS"].splitlines():
    if root and under(abs_, os.path.realpath(root)):
        print(abs_)
        sys.exit(0)
sys.exit(1)
PYEOF
}

# _classify_read_tool <path> [wt] -> print "APPROVE" or "ESCALATE<TAB><reason>" for a Read of
# <path>. APPROVE only when <path> is a CLEAN inert path, confined to the repo family, and not
# secret-like. rc is always 0 (the verdict is on stdout, like classify_permission).
#
# The clean-path guard is load-bearing security, not cosmetics: extract_pending_command emits a
# Bash tool_use as its RAW command string in the same slot a Read emits "Read <file_path>", so a
# Bash command whose text starts with "Read " (e.g. `Read x.py; rm -rf ~`) would otherwise enter
# this lane and SKIP classify_permission's operator-split default-deny — auto-approving arbitrary
# shell. A genuine Read file_path is a single inert path, so any whitespace / shell metacharacter /
# operator / traversal makes the target unapprovable here (a false deny escalates — the safe
# direction). The secret class is then checked on BOTH the raw path and its resolved realpath, so
# an in-family symlink with a benign name (notes.txt -> deploy.pem) can't launder a key.
#
# Two properties of the whitespace rejection are load-bearing, DO NOT weaken blindly:
#   - It is a DENYLIST: safety rests on the reject set covering every shell control / expansion /
#     quoting metacharacter. Extend the set, never trim it.
#   - `*[[:space:]]*` rejects an embedded NEWLINE too (a case-glob matches it), closing the
#     newline-as-command-separator variant. If anyone ever relaxes this to allow spaced paths,
#     ONLY space/tab may be re-admitted — a re-allowed newline reopens the masquerade.
# Known limitation: a worktree whose ROOT path itself contains whitespace makes every family read
# non-clean, so the feature degrades to always-escalate for that checkout (safe, but silent).
_classify_read_tool() {
  local path="$1" wt="${2:-}" abs
  if [ -z "$path" ]; then
    printf 'ESCALATE\t%s\n' "Read with no target"
    return 0
  fi
  case "$path" in
    *[[:space:]]* | *';'* | *'&'* | *'|'* | *'$'* | *'`'* | '~'* | *'..'* \
      | *'{'* | *'}'* | *'*'* | *'?'* | *'['* | *']'* | *'"'* | *"'"* | *'\'* \
      | *'>'* | *'<'* | *'('* | *')'*)
      printf 'ESCALATE\t%s\n' "read target is not a clean path: $path"
      return 0 ;;
  esac
  if _broker_seg_secretlike "$path"; then
    printf 'ESCALATE\t%s\n' "secret-like read target: $path"
    return 0
  fi
  abs="$(_broker_read_in_family "$path" "$wt")" || {
    printf 'ESCALATE\t%s\n' "read outside the repo family: $path"
    return 0
  }
  if _broker_seg_secretlike "$abs"; then
    printf 'ESCALATE\t%s\n' "secret-like read target (resolved): $abs"
    return 0
  fi
  printf 'APPROVE\n'
}

# _permission_strip_wrapper <segment> -> echo the segment with any leading detach-wrapper command
# (nohup/setsid) removed, so the sanctioned `nohup ./scripts/spoke-push.sh …` detach reduces to the
# recognised exec/marker self-op (#282). DELIBERATELY narrower than the tier-2 _danger_strip_prefix:
# it does NOT peel `env`/`command`/`NAME=value`. On the APPROVE side a leading `GIT_DIR=…` / `env …`
# CHANGES what an otherwise-safe verb does (redirecting a git op at a sibling repo, #282 review), so
# stripping it would launder a boundary crossing into a self-op. Only the output-agnostic nohup /
# setsid detach wrappers -- which never change WHICH files a command touches -- are peeled.
_permission_strip_wrapper() {
  local seg="$1" first
  while :; do
    seg="${seg#"${seg%%[![:space:]]*}"}"
    first="${seg%%[[:space:]]*}"
    case "$first" in
      nohup | setsid) seg="${seg#"$first"}" ;;
      *) break ;;
    esac
  done
  printf '%s' "${seg#"${seg%%[![:space:]]*}"}"
}

# _permission_redirect_scan <cmd> -> tokenize <cmd> (shlex) and print, one line each:
#   R<TAB><target>   for every FILE redirect target (>, >>, < ; glued `>f` or spaced `> f`), and
#   C<TAB><cleaned>  the command with every redirect operator+target removed, tokens rejoined
#                    with single spaces (NOT shell-quoted, so a trailing background `&` survives
#                    as a bare operator for the caller's textual operator-split).
# STRICT / deny-lean (#282 review): this drives an APPROVE, so it recognises ONLY the simple,
# fully-understood redirect forms and prints a lone `__UNPARSEABLE__` on ANYTHING else -- process
# substitution `>(cmd)`/`<(cmd)` (which EXECUTES a command), here-docs/here-strings (`<<`/`<<<`),
# and the `>&FILE` write-both-to-a-file form (whose target the naive scanner skipped as an fd-dup
# and never validated). A pure fd-duplication (`2>&1`, `>&2`, `>&-`) carries no file target and is
# dropped. Deliberately SEPARATE from the tier-2 _danger_redirect_targets (a best-effort target
# LIST for the DENY side): this one also emits the cleaned command and BAILS on the unknown, so
# they cannot share one body -- keep the two redirect forms they each recognise in sync by hand.
# No python3 -> no output (caller treats an unverifiable redirect as deny). Plain ASCII heredoc.
_permission_redirect_scan() {
  command -v python3 >/dev/null 2>&1 || return 0
  _PERM_CMD="$1" python3 2>/dev/null <<'PYEOF'
import os, re, shlex

cmd = os.environ["_PERM_CMD"]


def bail():
    print("__UNPARSEABLE__"); raise SystemExit(0)


# shlex.split flattens command STRUCTURE the space-rejoin cannot faithfully rebuild, so bail on the
# two compound forms that would otherwise launder a second command / an out-of-tree write past the
# tier-1 vouch (#282 re-review):
#   * a NEWLINE separates commands but is whitespace shlex silently eats -- `cat x >log\nrm -rf ~`
#     would rejoin to one segment `cat x rm -rf ~` and skip the operator-split + judge;
#   * a `cd` changes the base dir for a LATER redirect target, which this raw-command validation
#     (cwd=$wt, before the caller's cd-tracking) cannot follow -- `cd sub && echo x >link/out` would
#     validate link/out against $wt while the shell writes it under sub/ (a symlink escape).
# Both escalate (safe): the sanctioned detach shapes are a single command with no cd.
if "\n" in cmd:
    bail()
try:
    toks = shlex.split(cmd)
except Exception:
    bail()
if "cd" in toks:
    bail()


# A token that STARTS like a redirect operator: an optional fd number or `&`, then `>`/`>>`/`<`,
# then an optional `|` (the noclobber-override `>|` form -- dropping it would validate the literal
# `|` as the target and let `>|OUT-OF-TREE` slip past, #282 review). Matches _danger_redirect_targets.
redirish = re.compile(r"^([0-9]*|&)(>>?|<)\|?")

# shlex.split does NOT split on shell operators (`;`, `&`, `|`) or subshell parens, so a target
# GLUED to one (`>log;curl evil`, `>log&id`, `>a|b`) is absorbed into the "target" token and the
# trailing command would be laundered into a vouched verb's args. A real redirect target never
# holds these -- treat any as a bail (the `<`/`>`/`$`/backtick/quote/glob/`..` set is caught
# downstream by _broker_resolve_in_roots; this covers the operator chars it omits).
bad_target = re.compile(r"[;&|()`]")


def take_target(tgt):
    if bad_target.search(tgt):
        bail()
    targets.append(tgt)


targets = []
cleaned = []
i = 0
n = len(toks)
while i < n:
    t = toks[i]
    # Process substitution EXECUTES a command -- never a simple redirect. Bail on `>(` / `<(`
    # anywhere in the token (a glued `>(cmd` shlex-tokenises as one word).
    if "<(" in t or ">(" in t:
        bail()
    m = redirish.match(t)
    if not m:
        cleaned.append(t)
        i += 1
        continue
    rest = t[m.end():]
    # A here-doc / here-string (`<<word`, `<<<word`): redirish consumed one `<`, the next char is
    # another `<`. Not a file redirect -- bail.
    if rest.startswith("<"):
        bail()
    if rest.startswith("&"):
        fd = rest[1:]
        # A pure fd-duplication has ONLY digits (optionally a trailing `-`) or a bare `-` after the
        # `&`. Anything else is `>&FILE` (write both streams to a FILE) -- bail so its out-of-tree
        # target is never stripped-and-vouched unvalidated.
        if re.fullmatch(r"[0-9]+-?|-", fd):
            i += 1
            continue
        bail()
    if rest:
        take_target(rest)             # a glued file target (>file, 2>>file, &>file, >|file)
        i += 1
        continue
    # A bare operator token (`>`, `2>>`, `<`, `>|`): the target is the NEXT token, which must be a
    # plain filename (not another operator, fd-dup, process substitution, or operator-glued word).
    if i + 1 >= n:
        bail()
    nxt = toks[i + 1]
    if nxt.startswith("&") or nxt.startswith("(") or redirish.match(nxt):
        bail()
    take_target(nxt)
    i += 2

for t in targets:
    print("R\t" + t)
print("C\t" + " ".join(cleaned))
PYEOF
}

# _permission_redirects_ok <cmd> <cwd> <wt> <slug> <tasks> -> when EVERY file redirect target in
# <cmd> resolves inside the worktree/scratchpad AND is not secret-like, print the command with all
# redirects stripped and return 0; else return 1 (an out-of-tree / secret-like / unparseable
# target, or no python3). Each target is checked with _broker_seg_secretlike (a `>` clobbers or a
# `<` feeds it, so a secret target is rejected exactly like the mutation/exec/marker lanes reject
# theirs, #282 review) THEN _broker_resolve_in_roots (rejects `..`, absolute, `.git`, metachars).
# classify_permission calls this on the RAW command so the vouched (cleaned) command carries no
# redirect operator to be shattered by the `&`-split.
_permission_redirects_ok() {
  local cmd="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" out tag val cleaned="" saw_cleaned=0
  out="$(_permission_redirect_scan "$cmd")"
  [ -n "$out" ] || return 1                          # no python3 -> unverifiable -> deny
  case "$out" in *__UNPARSEABLE__*) return 1 ;; esac
  while IFS=$'\t' read -r tag val; do
    case "$tag" in
      R) [ -n "$val" ] || continue
         _broker_seg_secretlike "$val" && return 1
         _broker_resolve_in_roots "$val" "$cwd" "$wt" "$slug" "$tasks" >/dev/null 2>&1 || return 1 ;;
      C) cleaned="$val"; saw_cleaned=1 ;;
    esac
  done <<< "$out"
  [ "$saw_cleaned" -eq 1 ] || return 1
  printf '%s\n' "$cleaned"
}

# classify_permission <command> [worktree] -> "APPROVE" or "ESCALATE<TAB><reason>".
# DEFAULT-DENY: the command is APPROVEd only when EVERY segment (split on ; && || |) is a
# safe scoped self-op, so a single risky segment in a chain escalates the whole. When the
# spoke's <worktree> is known, the compound is DECOMPOSED and `cd` is tracked so the benign
# in-worktree mutation lane (#203, finding 4) can approve writes confined to the worktree or
# its scratchpad. Anything unrecognised — main-touching, force-push, history rewrite, an
# out-of-tree deletion, network fetch, browser/computer/mcp tool, or a bare non-Bash tool
# name — ESCALATEs, naming the offending command so the block record is actionable.
classify_permission() {
  local cmd="$1" wt="${2:-}" norm seg saw_seg=0 cwd="" slug="" tasks="" target new_cwd splitcmd
  if [ -n "$wt" ]; then
    slug="$(printf '%s' "$wt" | sed 's/[^A-Za-z0-9]/-/g')"
    tasks="${AFK_TASKS_ROOT:-/private/tmp}"
    cwd="$wt"                                       # the compound starts in the worktree
  fi
  # A non-Bash READ tool invocation arrives as "Read <file_path>" (extract_pending_command
  # carries the target). It is decided ENTIRELY by the read lane (#181), BEFORE operator-
  # splitting so a path with shell-ish characters is never chopped into bogus segments.
  case "$cmd" in
    'Read '*) _classify_read_tool "${cmd#Read }" "$wt"; return 0 ;;
  esac
  # Redirection (#282): validate on the WHOLE raw command (mirroring classify_danger:2533) then
  # STRIP it, so an in-tree log redirect no longer blocks Tier 1 and the `&`-split below never
  # shatters a `2>&1` into a bogus `1` segment. Every file target must resolve in-tree (else
  # escalate); the trailing `$(`/backtick reject still fires per segment on the cleaned command.
  # Inert without a worktree — an unverifiable redirect stays rejected by _permission_seg_safe.
  splitcmd="$cmd"
  case "$cmd" in
    *'>'* | *'<'*)
      if [ -n "$wt" ] && splitcmd="$(_permission_redirects_ok "$cmd" "$cwd" "$wt" "$slug" "$tasks")"
      then :
      else printf 'ESCALATE\t%s\n' "risky or unrecognised command: $cmd"; return 0
      fi ;;
  esac
  # Normalise the shell operators to newlines, longest first so `||` is not split by `|`
  # and `&&` is not split by a single `&`. The single `&` (background) MUST also split, or
  # `echo x & rm -rf /` would match the safe `echo ` prefix and never inspect the tail.
  norm="${splitcmd//&&/$'\n'}"
  norm="${norm//&/$'\n'}"
  norm="${norm//||/$'\n'}"
  norm="${norm//|/$'\n'}"
  norm="${norm//;/$'\n'}"
  while IFS= read -r seg; do
    seg="${seg#"${seg%%[![:space:]]*}"}"           # ltrim
    seg="${seg%"${seg##*[![:space:]]}"}"           # rtrim
    [ -n "$seg" ] || continue
    # Strip a leading nohup/setsid detach wrapper before the lane dispatch (#282): the sanctioned
    # `nohup ./scripts/spoke-push.sh …` detach otherwise reads its verb as `nohup` and misses the
    # exec/marker lanes. NARROWER than the tier-2 strip on purpose — env/`NAME=value` are NOT peeled
    # here (they can redirect a "safe" git verb at another repo, #282 review). Safe by construction:
    # classify_danger runs FIRST under the wall and denies a dangerous op behind the wrapper.
    seg="$(_permission_strip_wrapper "$seg")"
    [ -n "$seg" ] || continue                      # a bare wrapper (`nohup` alone) → nothing to vouch
    saw_seg=1
    # cd-tracking within the compound: a `cd` into a path that stays under the worktree/
    # scratchpad updates the current dir for the following segments' relative paths; a `cd`
    # that escapes (or a bare `cd` → $HOME, or no worktree context) escalates the whole.
    case "$seg" in
      'cd '*)
        target="${seg#cd }"; target="${target#"${target%%[![:space:]]*}"}"
        # An empty target (`cd` → $HOME) or a `-`-prefixed one (`cd -`/`--`/`-P`/`-L` → $OLDPWD
        # or $HOME) navigates OUT of the tree — never a literal in-tree dir. Reject before the
        # resolver, which would otherwise read `--` as an in-tree directory name and track a
        # bogus cwd. A real dir starting with `-` is always reachable as `./-x`.
        case "$target" in '' | -*) printf 'ESCALATE\t%s\n' "risky or unrecognised command: $cmd"; return 0 ;; esac
        if [ -n "$wt" ] && new_cwd="$(_broker_resolve_in_roots "$target" "$cwd" "$wt" "$slug" "$tasks")"; then
          cwd="$new_cwd"; continue
        fi
        printf 'ESCALATE\t%s\n' "risky or unrecognised command: $cmd"; return 0 ;;
    esac
    if ! _permission_seg_safe "$seg" "$cwd" "$wt" "$slug" "$tasks"; then
      printf 'ESCALATE\t%s\n' "risky or unrecognised command: $cmd"
      return 0
    fi
  done <<< "$norm"
  # An empty / all-whitespace command has no segment to vouch for — never approve nothing.
  [ "$saw_seg" -eq 1 ] || { printf 'ESCALATE\t%s\n' "empty or unreadable command"; return 0; }
  printf 'APPROVE\n'
}
