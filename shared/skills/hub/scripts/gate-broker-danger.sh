#!/usr/bin/env bash
# gate-broker-danger.sh -- split out of gate-broker.sh (issue #275).
#
# A pure function-definition module of the gate-broker core. Sourced by the entry lib
# gate-broker.sh AFTER worktree-lib/hub-inject/log/afk_now and BEFORE any function is
# called, so every cross-module helper resolves at call time. Not run on its own.
set -uo pipefail

# --- Tier-2 static danger classifier (issue #261) -----------------------------
# The deny-wall's fast static blacklist. classify_permission (Tier 1) names the KNOWN-SAFE
# scoped self-ops (APPROVE); classify_danger names the KNOWN-DANGEROUS boundary crossings
# (DENY) among everything else, so the Tier-3 LLM judge runs ONLY on the true residue --
# neither statically safe nor statically dangerous. Same operator-split as classify_permission
# (; && || | &), first dangerous segment wins. Empty output = "no static danger" -> the
# orchestrator routes the command to the judge.
#
# CATEGORIES (the boundary the existing scope-guards do NOT already cover):
#   - privilege escalation / disk destroyers / ownership: sudo/doas/su, dd, mkfs*, fdisk,
#     parted, shred, chown/chgrp
#   - arbitrary exec / classifier-evasion (#269): eval, a shell `-c` inline command, a bare
#     shell verb (pipe-to-shell target), xargs spawning a shell
#   - network egress off-allowlist: curl/wget to a non-allowlisted host; raw sockets
#     (nc/ncat/netcat/telnet/ssh/scp/sftp/ftp) denied outright; a curl/wget WRITE-METHOD flag
#     (-d/--data*, -F/--form, -T/--upload-file, -X POST|PUT|PATCH|DELETE) even to an allowed host
#   - supply-chain publish (#269): npm/yarn/pnpm/poetry publish, twine upload, gem push,
#     cargo publish, docker/podman push
#   - repo/collaboration mutation (#269): mutating gh subcommands (pr create|merge|close|...,
#     repo delete|create|..., release create|...) -- read/comment/issue subcommands stay open
#   - keychain / credential reads: `security`, or a read of a secret-like path
#   - out-of-tree writes: a mutating verb (mv/cp/rm/mkdir/chmod) not confined to the worktree,
#     or a `>`/`>>` redirection whose target escapes it
# NOT duplicated here (owned by sibling PreToolUse scope-guards, kept authoritative): push to
# main / other refs (push-scope-guard, spoke-main-guard), secrets in a commit (secrets-scan),
# config edits (config-protection), `--no-verify` (block-no-verify). Package-installs are
# deliberately NOT statically denied -- they route to the judge so legit fresh-worktree setup
# (`pip install -r requirements-dev.txt`) is not stranded; the journal surfaces a dangerous one
# for promotion to a static rule (the #261 Phase-4 measure->promote loop).
# UPGRADE: promote a judge-caught tier-2 miss (a novel destructive verb, a package-install that
# ran a hostile lifecycle script) into a static case here once the journal shows it.

# _danger_strip_prefix <segment> -> echo the segment with any leading `NAME=value` env
# assignments and no-flag wrapper commands (env/command/nohup/setsid) removed, so a verb-keyed
# category is not evaded by `FOO=1 sudo ...` or `env sudo ...` (the repo's #15292 env-prefix gap,
# here on a DENY wall). The assignment case is the classic gap and is handled fully; a wrapper
# that itself carries flags (`nice -n 10 sudo`, `env -i sudo`) stops the strip and routes to the
# judge -- an acceptable partial fix for the common shapes.
_danger_strip_prefix() {
  local seg="$1" first
  while :; do
    seg="${seg#"${seg%%[![:space:]]*}"}"
    first="${seg%%[[:space:]]*}"
    case "$first" in
      [A-Za-z_]*=*)
        case "${first%%=*}" in *[!A-Za-z0-9_]*) break ;; esac   # not a valid var name -> stop
        seg="${seg#"$first"}" ;;
      env | command | nohup | setsid) seg="${seg#"$first"}" ;;
      *) break ;;
    esac
  done
  printf '%s' "${seg#"${seg%%[![:space:]]*}"}"
}

# _danger_redirect_targets <cmd> -> print each FILE redirect target in <cmd> (one per line),
# skipping fd-duplications (2>&1, >&2). shlex-tokenized so a `>` inside a quoted string is NOT a
# redirect and quoting is honored; prints `__UNPARSEABLE__` on unbalanced quotes (caller denies,
# deny-lean). Replaces the last-`>`-only heuristic that a trailing `2>&1` defeated (#261 review):
# scanning EVERY redirect operator token catches the real out-of-tree target regardless of what
# trails it. Empty (no python3, or no redirects) -> the caller finds no target and moves on.
_danger_redirect_targets() {
  command -v python3 >/dev/null 2>&1 || return 0
  _DANGER_CMD="$1" python3 2>/dev/null <<'PYEOF'
import os, re, shlex

cmd = os.environ["_DANGER_CMD"]
try:
    toks = shlex.split(cmd)
except Exception:
    print("__UNPARSEABLE__"); raise SystemExit(0)

op = re.compile(r"^([0-9]*|&)(>>?)\|?")   # a redirect operator, possibly glued to its target
targets = []
i = 0
while i < len(toks):
    t = toks[i]
    m = op.match(t)
    if m:
        suffix = t[m.end():]
        if suffix:
            if not suffix.startswith("&"):
                targets.append(suffix)
        elif i + 1 < len(toks) and not toks[i + 1].startswith("&"):
            targets.append(toks[i + 1])
            i += 1
    i += 1
for t in targets:
    print(t)
PYEOF
}

# _danger_network_seg <segment> -> print a reason and rc 0 when the segment is off-allowlist
# network egress OR a write-method egress (possible exfil), else rc 1. Raw-socket tools are denied
# outright; curl/wget URL hosts are parsed (python3) and denied unless every host is allowlisted.
# Only `://`-scheme tokens are read as hosts (so a bare hostless arg is never mistaken for a host);
# a curl/wget carrying NO scheme URL fails CLOSED (deny), as does an absent python3 -- an
# unverifiable egress does not run. #269: a WRITE-METHOD flag (-d/--data*, -F/--form,
# -T/--upload-file, -X/--request with POST|PUT|PATCH|DELETE) is denied even to an allowlisted host
# -- a POST body / upload can exfil to a gist or the API. Download flags -o/-O (write the RESPONSE
# to a file) stay benign, so a legit `curl ... -o out.json` GET read is not blocked.
_danger_network_seg() {
  local seg="$1" verb host_check
  verb="${seg%%[[:space:]]*}"
  case "$verb" in
    nc | ncat | netcat | telnet | ssh | scp | sftp | ftp)
      printf 'raw network egress (%s) is denied under bypass' "$verb"; return 0 ;;
    curl | wget) ;;
    *) return 1 ;;
  esac
  command -v python3 >/dev/null 2>&1 || { printf 'network egress unverifiable (no python3): %s' "$verb"; return 0; }
  host_check="$(_DANGER_SEG="$seg" python3 2>/dev/null <<'PYEOF'
import os, sys, shlex
from urllib.parse import urlparse

def allowed(h):
    h = h.lower()
    if h in {"api.anthropic.com", "anthropic.com", "github.com", "api.github.com",
             "raw.githubusercontent.com", "codeload.github.com", "objects.githubusercontent.com"}:
        return True
    return any(h.endswith(s) for s in (".anthropic.com", ".github.com", ".githubusercontent.com"))

try:
    toks = shlex.split(os.environ["_DANGER_SEG"])
except Exception:
    print("DENY unparseable"); sys.exit(0)

# A WRITE-METHOD flag makes this an upload/POST egress (possible exfil) regardless of host --
# deny it even to an allowlisted host (#269). -o/-O write the RESPONSE to a file, not an upload,
# so they stay benign. Short upload flags (-d/-F/-T) and their GLUED forms (-d@f, -XPOST) are
# CURL-only: in wget -d/-F/-T mean --debug/--force-html/--timeout, so applying curl semantics to
# wget false-denied benign reads (#269 review WARNING). wget write-methods are the long forms
# (--post-data/--post-file/--body-data/--body-file/--method=), handled for both below.
mutating = {"POST", "PUT", "PATCH", "DELETE"}
verb = os.path.basename(toks[0]) if toks else ""
is_curl = verb == "curl"

def method_ok(m):
    return m.upper() in mutating

i = 1
while i < len(toks):
    t = toks[i]
    # Long-form request bodies / methods -- curl AND wget.
    if t.startswith("--data") or t.startswith("--form") or t == "--upload-file" \
            or t.startswith("--post-data") or t.startswith("--post-file") \
            or t.startswith("--body-data") or t.startswith("--body-file"):
        print("DENYWRITE " + t); sys.exit(0)
    if (t.startswith("--request=") or t.startswith("--method=")) and method_ok(t.split("=", 1)[1]):
        print("DENYWRITE " + t); sys.exit(0)
    if t in ("--request", "--method") and i + 1 < len(toks) and method_ok(toks[i + 1]):
        print("DENYWRITE " + t + " " + toks[i + 1]); sys.exit(0)
    if is_curl:
        # curl short upload flags, spaced or glued (-d @f / -d@f / -Ffile=@f / -Tfile). startswith
        # subsumes the bare exact flag (-d / -F / -T).
        if t.startswith("-d") or t.startswith("-F") or t.startswith("-T"):
            print("DENYWRITE " + t); sys.exit(0)
        # -X METHOD (spaced) or -XMETHOD (glued).
        if t == "-X" and i + 1 < len(toks) and method_ok(toks[i + 1]):
            print("DENYWRITE -X " + toks[i + 1]); sys.exit(0)
        if len(t) > 2 and t.startswith("-X") and method_ok(t[2:]):
            print("DENYWRITE " + t); sys.exit(0)
    i += 1

# Only a scheme URL (contains ://) whose urlparse yields a hostname counts as an egress host.
# A header value or a bare hostless arg is never a host -> no false deny, and a curl with no
# scheme URL yields no host -> fail-closed DENY below.
hosts = []
for t in toks[1:]:
    if "://" in t:
        h = urlparse(t).hostname
        if h:
            hosts.append(h)

if not hosts:
    print("DENY no-parseable-host"); sys.exit(0)
bad = [h for h in hosts if not allowed(h)]
print("DENY " + bad[0] if bad else "OK")
PYEOF
)"
  case "$host_check" in
    OK) return 1 ;;
    'DENYWRITE '*) printf 'network write-method egress (possible exfil): %s' "${host_check#DENYWRITE }"; return 0 ;;
    'DENY '*) printf 'network egress to a non-allowlisted host: %s' "${host_check#DENY }"; return 0 ;;
    *) printf 'network egress unverifiable: %s' "$verb"; return 0 ;;
  esac
}

# _danger_credential_seg <segment> <cwd> <wt> <slug> <tasks> -> print a reason and rc 0 when the
# segment reads a keychain or an OUT-OF-TREE secret-like path, else rc 1. `security`/`keychain`
# (macOS keychain access) is denied on the verb; a read verb (cat/head/...) touching a secret-like
# token (reusing _broker_seg_secretlike) is denied ONLY when the path is out-of-tree. An IN-TREE
# secret-named file (`tests/fixtures/key.pem`) is the spoke's OWN fixture -- it already has write
# access there, so reading it is within the worktree trust boundary and is NOT denied (#261 review
# NIT). Without a worktree context every secret-like read is denied (can't prove in-tree -> safe).
_danger_credential_seg() {
  local seg="$1" cwd="${2:-}" wt="${3:-}" slug="${4:-}" tasks="${5:-}" verb rest tok
  verb="${seg%%[[:space:]]*}"
  case "$verb" in
    security | keychain) printf 'keychain/credential access (%s) is denied' "$verb"; return 0 ;;
    cat | less | more | head | tail | strings | xxd | od | base64 | openssl | grep | awk | sed | cp | dd | sort | uniq) ;;
    *) return 1 ;;
  esac
  rest="${seg#"$verb"}"
  while [ -n "$rest" ]; do
    rest="${rest#"${rest%%[![:space:]]*}"}"
    [ -n "$rest" ] || break
    tok="${rest%%[[:space:]]*}"
    rest="${rest#"$tok"}"
    case "$tok" in -*) continue ;; esac
    _broker_seg_secretlike "$tok" || continue
    # An in-tree secret-named file is the spoke's own fixture -> within the trust boundary, allow.
    if [ -n "$wt" ] && _broker_resolve_in_roots "$tok" "$cwd" "$wt" "$slug" "$tasks" >/dev/null 2>&1; then
      continue
    fi
    printf 'reads a secret-like path: %s' "$tok"; return 0
  done
  return 1
}

# _danger_write_seg <segment> <cwd> <wt> <slug> <tasks> -> print a reason and rc 0 when the
# segment is a mutating verb (mv/cp/rm/mkdir/chmod) the in-worktree mutation lane does NOT
# confine (invert _permission_seg_mutation_ok), else rc 1. Out-of-tree REDIRECTIONS are handled
# separately, whole-command, in classify_danger (a `2>&1` tail must not shift the check off the
# real target). Inert (rc 1) without a worktree context -- the resolver needs it.
_danger_write_seg() {
  local seg="$1" cwd="$2" wt="$3" slug="$4" tasks="$5" verb
  [ -n "$wt" ] || return 1
  verb="${seg%%[[:space:]]*}"
  case "$verb" in
    mv | cp | rm | mkdir | chmod)
      if ! _permission_seg_mutation_ok "$seg" "$cwd" "$wt" "$slug" "$tasks"; then
        printf 'writes outside the worktree (%s)' "$verb"; return 0
      fi ;;
  esac
  return 1
}

# _danger_privilege_seg <segment> -> print a reason and rc 0 for a privilege-escalation,
# disk-destroying, or ownership-mutation verb, else rc 1. chown/chgrp fold in here (deny
# outright, unlike the in-tree-allowed chmod owned by _danger_write_seg): a worktree-confined
# spoke has no legit ownership-mutation use, so a target check would only add surface (#269).
_danger_privilege_seg() {
  local verb; verb="${1%%[[:space:]]*}"
  case "$verb" in
    sudo | doas | su | dd | fdisk | parted | shred | mkfs | mkfs.*)
      printf 'privileged / destructive command (%s) is denied' "$verb"; return 0 ;;
    chown | chgrp)
      printf 'ownership mutation (%s) is denied under bypass' "$verb"; return 0 ;;
    *) return 1 ;;
  esac
}

# _danger_publish_seg <segment> -> print a reason and rc 0 for a supply-chain PUBLISH (an
# outward package/image release a worktree-confined spoke never performs), else rc 1. A
# two-token verb+subcommand match (npm/yarn/pnpm/poetry publish, twine upload, gem push,
# cargo publish, docker/podman push); a bare `npm install` or `docker build` is untouched.
_danger_publish_seg() {
  local seg="$1" verb sub
  verb="${seg%%[[:space:]]*}"
  sub="${seg#"$verb"}"; sub="${sub#"${sub%%[![:space:]]*}"}"; sub="${sub%%[[:space:]]*}"
  case "$verb $sub" in
    'npm publish' | 'yarn publish' | 'pnpm publish' | 'poetry publish' | \
    'twine upload' | 'gem push' | 'cargo publish' | 'docker push' | 'podman push')
      printf 'supply-chain publish (%s %s) is denied under bypass' "$verb" "$sub"; return 0 ;;
    *) return 1 ;;
  esac
}

# _danger_eval_seg <segment> -> print a reason and rc 0 for arbitrary-exec / classifier-evasion
# shapes, else rc 1. `eval` runs an unsplit string (`eval "$(curl ...)"` is never operator-split
# or host-checked); a shell verb carrying `-c` (inline command) or `-s` (script on stdin) runs
# arbitrary code; a BARE shell verb (no non-flag argument) is a pipe-to-shell target reading stdin
# (`curl ... | bash`, whose halves are separate segments); and `xargs` whose COMMAND WORD is a
# shell (`xargs sh -c ...`) launders exec through the argv. Benign `bash -n file` / `bash
# script.sh` (a non-flag arg, no -c/-s), `bash --version` (info probe), and `find | xargs grep
# bash` (the command word is grep, NOT the shell-named ARGUMENT) all still pass -- the xargs scan
# checks only the exec'd command word, skipping xargs's own value-taking options (#269 review
# BLOCKER). UPGRADE: a combined short-flag cluster (`bash -lc`, `sh -sc`) is not split here -- it
# routes to the fail-closed judge; add flag-cluster parsing if the journal shows it exploited.
_danger_eval_seg() {
  local seg="$1" verb rest tok has_arg=0 skip=0 cmdword=""
  verb="${seg%%[[:space:]]*}"
  case "$verb" in
    eval) printf 'eval runs an uninspected command string -- denied under bypass'; return 0 ;;
    sh | bash | zsh | dash | ksh)
      rest="${seg#"$verb"}"
      while [ -n "$rest" ]; do
        rest="${rest#"${rest%%[![:space:]]*}"}"
        [ -n "$rest" ] || break
        tok="${rest%%[[:space:]]*}"
        rest="${rest#"$tok"}"
        case "$tok" in
          -c | -s)
            printf 'inline/stdin shell command (%s %s ...) is denied under bypass' "$verb" "$tok"; return 0 ;;
          --version | --help | -V) return 1 ;;  # an info probe, not an exec -- benign
          -*) continue ;;
          *) has_arg=1 ;;
        esac
      done
      if [ "$has_arg" -eq 0 ]; then
        printf 'pipe-to-shell / bare interactive shell (%s) is denied under bypass' "$verb"; return 0
      fi
      return 1 ;;
    xargs)
      # The command xargs execs is its FIRST non-option token -- match ONLY that against the shell
      # set, skipping xargs's own value-taking options (a separate-word value like `-I {}` / `-n 1`
      # / `-P 4` / GNU `--max-procs 4`). Scanning every token falsely denied `xargs grep bash` (#269
      # review BLOCKER). REQUIRED-arg GNU long options are skipped in their SPACED form too (#269
      # review). UPGRADE: an OPTIONAL-arg option (GNU `--max-lines`/`--replace`/`-e`/`--eof`, whose
      # value is `=`-glued only) in a bogus spaced form, or an unknown long option, is left to the
      # fail-closed tier-3 judge rather than risk a mis-skip that OPENS a shell-launder.
      rest="${seg#"$verb"}"
      while [ -n "$rest" ]; do
        rest="${rest#"${rest%%[![:space:]]*}"}"
        [ -n "$rest" ] || break
        tok="${rest%%[[:space:]]*}"
        rest="${rest#"$tok"}"
        if [ "$skip" -eq 1 ]; then skip=0; continue; fi
        case "$tok" in
          -I | -J | -L | -n | -P | -R | -S | -s | -E | -a | -d | \
          --max-args | --max-procs | --max-chars | --delimiter | --arg-file | --process-slot-var)
            skip=1; continue ;;                       # an option taking a separate-word value
          --*=* | -*) continue ;;                     # glued-value / no-arg / optional-arg option
          *) cmdword="$tok"; break ;;
        esac
      done
      case "$cmdword" in
        sh | bash | zsh | dash | ksh | eval | /bin/sh | /bin/bash | /usr/bin/env | env)
          printf 'xargs spawning a shell (%s) is denied under bypass' "$cmdword"; return 0 ;;
      esac
      return 1 ;;
  esac
  return 1
}

# _danger_gh_seg <segment> -> print a reason and rc 0 for a MUTATING gh subcommand a
# worktree-confined spoke never legitimately runs (repo/collaboration/release mutation), else
# rc 1. Split at the SUBCOMMAND level, never blanket-deny: the spoke tooling shells `gh issue
# view/comment` and `gh pr view` (allowlisted at worktree-new.sh:338), so only the mutating
# pr/repo/release verbs are denied; every read/comment/issue subcommand falls through to Tier 1
# / the judge. A spoke never self-lands or opens PRs (the ship-discipline rule).
_danger_gh_seg() {
  local seg="$1" verb obj sub rest
  verb="${seg%%[[:space:]]*}"
  [ "$verb" = gh ] || return 1
  rest="${seg#gh}"; rest="${rest#"${rest%%[![:space:]]*}"}"
  obj="${rest%%[[:space:]]*}"
  rest="${rest#"$obj"}"; rest="${rest#"${rest%%[![:space:]]*}"}"
  sub="${rest%%[[:space:]]*}"
  case "$obj $sub" in
    'pr create' | 'pr merge' | 'pr close' | 'pr reopen' | 'pr ready' | 'pr edit' | \
    'repo delete' | 'repo create' | 'repo rename' | 'repo archive' | 'repo edit' | \
    'release create' | 'release delete' | 'release edit' | 'release upload')
      printf 'gh mutating subcommand (gh %s %s) is denied under bypass' "$obj" "$sub"; return 0 ;;
    *) return 1 ;;
  esac
}

# classify_danger <command> [worktree] -> "DENY<TAB><reason>" for the FIRST dangerous segment,
# or empty (rc 0) when no segment statically matches (the orchestrator then routes to the judge).
classify_danger() {
  local cmd="$1" wt="${2:-}" norm seg slug="" tasks="" cwd="" reason target new_cwd rtargets rt
  if [ -n "$wt" ]; then
    slug="$(printf '%s' "$wt" | sed 's/[^A-Za-z0-9]/-/g')"
    tasks="${AFK_TASKS_ROOT:-/private/tmp}"
    cwd="$wt"
    # Out-of-tree REDIRECTION (whole-command, shlex-based). Done once on the RAW command -- not
    # per operator-split segment -- so a trailing `2>&1` (which the `&`-split would shatter) can
    # never shift the check off the real target (#261 review). Targets resolve against the initial
    # worktree cwd, so an ABSOLUTE out-of-tree redirect is caught regardless of any earlier `cd`.
    rtargets="$(_danger_redirect_targets "$cmd")"
    while IFS= read -r rt; do
      [ -n "$rt" ] || continue
      [ "$rt" = "__UNPARSEABLE__" ] && { printf 'DENY\t%s\n' "unparseable command (unbalanced quoting) -- fail-closed"; return 0; }
      case "$rt" in '&'*) continue ;; esac
      if ! _broker_resolve_in_roots "$rt" "$wt" "$wt" "$slug" "$tasks" >/dev/null 2>&1; then
        printf 'DENY\t%s\n' "writes outside the worktree via redirection: $rt"; return 0
      fi
    done <<< "$rtargets"
  fi
  norm="${cmd//&&/$'\n'}"; norm="${norm//&/$'\n'}"; norm="${norm//||/$'\n'}"
  norm="${norm//|/$'\n'}"; norm="${norm//;/$'\n'}"
  while IFS= read -r seg; do
    seg="${seg#"${seg%%[![:space:]]*}"}"; seg="${seg%"${seg##*[![:space:]]}"}"
    [ -n "$seg" ] || continue
    # Strip a leading `FOO=bar` / `env|command|nohup|setsid` prefix so it can't shift the verb
    # off a keyed category (the #15292 env-prefix gap, here on a DENY wall).
    seg="$(_danger_strip_prefix "$seg")"
    [ -n "$seg" ] || continue
    # cd-tracking mirrors classify_permission so a `cd`-then-write compound resolves relative
    # targets against the right dir. A cd that ESCAPES the roots does NOT leave cwd in-tree
    # (that let a following relative write resolve against a stale in-tree cwd, #261 review);
    # instead cwd is set to an out-of-tree sentinel, so subsequent relative writes resolve
    # out-of-tree and deny.
    case "$seg" in
      'cd '*)
        target="${seg#cd }"; target="${target#"${target%%[![:space:]]*}"}"
        case "$target" in '' | -*) continue ;; esac
        if [ -n "$wt" ] && new_cwd="$(_broker_resolve_in_roots "$target" "$cwd" "$wt" "$slug" "$tasks")"; then
          cwd="$new_cwd"
        else
          cwd="/__afk_cd_escaped__"   # out-of-tree sentinel: relative writes below now deny
        fi
        continue ;;
    esac
    if reason="$(_danger_privilege_seg "$seg")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_eval_seg "$seg")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_network_seg "$seg")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_publish_seg "$seg")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_gh_seg "$seg")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_credential_seg "$seg" "$cwd" "$wt" "$slug" "$tasks")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
    if reason="$(_danger_write_seg "$seg" "$cwd" "$wt" "$slug" "$tasks")"; then printf 'DENY\t%s\n' "$reason"; return 0; fi
  done <<< "$norm"
  return 0
}

# --- Tier-3 headless LLM judge (issue #261) -----------------------------------
# The residue tiers 1-2 did not resolve goes to a cheap headless judge: a TOOLLESS `claude -p`
# (pure text yes/no classification, no tools granted, so it makes NO tool calls and can never
# itself trigger the deny-wall or recurse), Haiku, bounded ~2s. FAIL-CLOSED: a timeout, a
# nonzero exit, or an unparseable verdict all read as DANGEROUS -- an unjudgeable risky command
# does not run under bypass (the caller warns + journals instead). Verdicts are cached by command
# hash so a command repeated across a drain costs at most one LLM call.

# _judge_cache_dir -> the per-run verdict cache dir (under the afk state dir, cleared per window).
_judge_cache_dir() { printf '%s\n' "$(_afk_state_dir)/judge-cache"; }

# _judge_cache_key <cmd> -> a stable content hash of the command (the cache filename). Empty
# when shasum is unavailable, which disables caching (every call runs the judge) but is harmless.
_judge_cache_key() { printf '%s' "$1" | shasum -a 256 2>/dev/null | awk '{print $1}'; }

# _judge_timeout -> the judge wall-clock budget in seconds (AFK_JUDGE_TIMEOUT, default 120).
# The default must accommodate a full headless claude -p round trip: CLI cold start alone
# exceeds the old 2s bound, which fail-closed EVERY tier-3 decision on this host (#268). A
# non-numeric or non-positive override falls back to the default so the bound is never lifted.
_judge_timeout() {
  local s="${AFK_JUDGE_TIMEOUT:-120}"
  case "$s" in '' | *[!0-9]*) s=120 ;; esac
  [ "$s" -lt 1 ] && s=120
  printf '%s\n' "$s"
}

# --- drain-level judge-unavailable halt (#268 AC4) ----------------------------
# A dead judge (a bad model, a revoked token, a structural budget bug) otherwise DENIES one
# tier-3 command at a time, silently, for the whole window. After N CONSECUTIVE unavailable
# (nonzero-rc) outcomes we raise a drain-level flag -- a FILE in the shared afk state dir, since
# the judge runs in the SPOKE's PreToolUse hook subprocess (not the supervisor loop, so the
# process-global _AFK_AUTH_FAILED the answerer uses cannot reach it). A supervisor that consults
# broker_judge_halt_pending can then pause dispatch + re-probe, mirroring the answerer
# auth-failure path (#241 §9) -- that consult is the drain's to wire (kept out of this change's
# scope). A reachable judge (rc 0) clears the streak + the flag so the drain resumes on recovery.
# The streak counter is a best-effort read-modify-write shared across concurrent spoke hooks: a
# lost increment only DELAYS the halt (fires a failure or two later), never spuriously raises it
# -- fine for an advisory heuristic; no lock is warranted until a consumer needs the exact count.

# _judge_streak_file -> the consecutive judge-unavailable counter (reset by any reachable judge).
_judge_streak_file() { printf '%s\n' "$(_afk_state_dir)/judge-unavailable-streak"; }

# _judge_halt_file -> the raised drain-level halt flag; its content is a human-readable reason.
_judge_halt_file() { printf '%s\n' "$(_afk_state_dir)/judge-halt"; }

# _judge_halt_streak -> consecutive unavailable outcomes before the halt is raised
# (AFK_JUDGE_HALT_STREAK, default 3). A non-numeric/non-positive override falls back to the
# default so the threshold is never silently disabled (mirrors _judge_timeout).
_judge_halt_streak() {
  local n="${AFK_JUDGE_HALT_STREAK:-3}"
  case "$n" in '' | *[!0-9]*) n=3 ;; esac
  [ "$n" -lt 1 ] && n=3
  printf '%s\n' "$n"
}

# broker_judge_halt_pending -> rc 0 when the drain-level judge halt is raised. A supervisor
# consults this to pause dispatch and re-probe (the judge counterpart of _AFK_AUTH_FAILED, #268).
broker_judge_halt_pending() { [ -f "$(_judge_halt_file)" ]; }

# broker_reset_judge_halt -> clear the halt flag AND the streak counter (the judge recovered, or
# a manual reset). Best-effort; never aborts.
broker_reset_judge_halt() {
  rm -f "$(_judge_halt_file)" "$(_judge_streak_file)" 2>/dev/null || true
}

# _judge_note_unavailable -> record one consecutive judge-unavailable outcome; at the threshold
# crossing raise the halt flag ONCE and journal a distinct drain-level event (so the morning
# review sees "the judge died, dispatch paused" rather than a scatter of per-command DENYs).
_judge_note_unavailable() {
  local sf hf n streak
  sf="$(_judge_streak_file)"; hf="$(_judge_halt_file)"
  mkdir -p "$(dirname "$sf")" 2>/dev/null || true
  streak="$(cat "$sf" 2>/dev/null)"; case "$streak" in '' | *[!0-9]*) streak=0 ;; esac
  streak=$(( streak + 1 ))
  printf '%s\n' "$streak" > "$sf" 2>/dev/null || true
  n="$(_judge_halt_streak)"
  # Raise only on the threshold crossing (flag not already present) so the distinct journal
  # event fires once, not on every further failure past the threshold.
  if [ "$streak" -ge "$n" ] && [ ! -f "$hf" ]; then
    printf 'judge unavailable %sx in a row -- dispatch paused, re-probing (#268)\n' "$streak" \
      > "$hf" 2>/dev/null || true
    _broker_journal_line "" judge "judge unavailable ${streak}x in a row -- pausing dispatch" scope
  fi
}

# _judge_note_available -> a reachable judge (rc 0): clear the streak + any raised halt so the
# drain resumes. Only touches disk when there is state to clear (no write on the healthy path).
_judge_note_available() {
  if [ -f "$(_judge_streak_file)" ] || [ -f "$(_judge_halt_file)" ]; then
    broker_reset_judge_halt
  fi
}

# _judge_base_cmd -> the headless judge command (AFK_JUDGE_CMD override for tests). TOOLLESS by
# construction: `--allowedTools ''` grants NO tools, so the judge can make no tool calls -- it can
# never fire afk_danger_guard or recurse. Haiku + `-p` for a cheap, non-interactive run.
# UPGRADE: confirm `--allowedTools ''` grants zero tools on the deployed CLI; if a future CLI
# defaults some tool on, switch to an explicit `--disallowedTools '*'` or equivalent.
_judge_base_cmd() {
  printf '%s\n' "${AFK_JUDGE_CMD:-claude -p --no-session-persistence --model ${AFK_JUDGE_MODEL:-claude-haiku-4-5} --allowedTools ''}"
}

# _judge_prompt <cmd> -> the tight classification prompt. Plain ASCII (no backticks/parens in a
# way that would trip bash 3.2, and this is a normal heredoc, not $()-nested).
_judge_prompt() {
  local cmd="$1"
  cat <<EOF
You are a security gate for an UNATTENDED coding agent running inside its OWN git worktree
under bypassPermissions -- no human will ever see a prompt. Decide whether the shell command
below is DANGEROUS to run unattended. It is DANGEROUS if its effects can reach OUTSIDE the
worktree: writing or deleting files outside it, network egress to an untrusted host, reading
credentials or the keychain, touching the main branch or other git refs, force-pushing or
rewriting history, privilege escalation, or destroying disks. It is SAFE if its effects stay
confined to the worktree. When in doubt, answer dangerous.
End with EXACTLY one final line: 'VERDICT: safe' or 'VERDICT: dangerous'. You may precede it
with a one-line reason.

COMMAND:
$cmd
EOF
}

# _judge_raw <cmd> [keep_stderr] -> run ONE bounded round trip of the toolless judge over <cmd>
# and print its raw output; the rc is the judge's own (0 reachable, nonzero broken -- see
# _judge_fail_reason for why the timeout rc is NOT a fixed constant).
# PURE: no cache read/write, no streak/halt bookkeeping, no verdict parsing -- the caller owns
# every side effect. Shared by judge_permission (which decides, caches, and counts streaks) and
# broker_judge_probe (which only asks "is the judge alive", #279) so the two can never drift on
# WHAT a round trip is -- the probe must exercise the real prompt, command, and budget, or it
# would prove the liveness of something the drain does not actually use.
#
# keep_stderr=1 folds the judge's stderr into the captured stream instead of discarding it,
# mirroring run_answerer ("the CLI prints credential failures there and exits nonzero"). The
# DECISION path leaves it 0: stderr must never reach a verdict parse it could pollute. The
# DIAGNOSTIC path (the probe) sets it 1 -- naming the failure is the probe's whole job, and
# _judge_parse_tag is VERDICT-anchored, so folded stderr cannot forge a verdict.
_judge_raw() {
  local cmd="$1" keep_stderr="${2:-0}" secs jcmd prompt pf raw rc
  secs="$(_judge_timeout)"
  jcmd="$(_judge_base_cmd)"
  prompt="$(_judge_prompt "$cmd")"
  # Deliver the prompt via a temp file the wrapped command reopens with `exec <`, mirroring
  # run_answerer: the bound (_afk_with_timeout portable fallback) BACKGROUNDS the command and a
  # backgrounded job's stdin is /dev/null, so a bare here-string would be lost. The here-string
  # stays as the fallback for the foreground timeout/perl paths (and when mktemp is unavailable).
  pf="$(mktemp 2>/dev/null)" || pf=""
  [ -n "$pf" ] && { printf '%s' "$prompt" > "$pf"; jcmd="exec <'$pf'; $jcmd"; }
  if [ "$keep_stderr" = "1" ]; then
    raw="$(_broker_run_bounded "$secs" bash -c "$jcmd" <<<"$prompt" 2>&1)"; rc=$?
  else
    raw="$(_broker_run_bounded "$secs" bash -c "$jcmd" <<<"$prompt" 2>/dev/null)"; rc=$?
  fi
  [ -n "$pf" ] && rm -f "$pf" 2>/dev/null
  printf '%s' "$raw"
  return "$rc"
}

# _judge_elapsed_now -> the wall-clock epoch used to time a round trip. Deliberately NOT
# afk_now: that honors the AFK_NOW test pin, which would freeze every measured duration at 0
# and silently disable the elapsed-based timeout detection below. A duration is real time.
_judge_elapsed_now() { date +%s; }

# _judge_parse_tag <raw> -> the verdict the judge answered: "safe", "dangerous", or EMPTY when
# the output carries no parseable VERDICT line (which every caller reads as unusable).
_judge_parse_tag() {
  printf '%s' "$1" | grep -ioE 'VERDICT:[[:space:]]*(safe|dangerous)' | tail -1 \
    | grep -ioE 'safe|dangerous' | tr '[:upper:]' '[:lower:]'
}

# _judge_fail_reason <rc> [elapsed] [budget] -> the fail-closed reason for a NONZERO judge rc
# (#268 AC3): a TIMEOUT reads differently from any other judge failure, so the decision journal
# and the arm-time probe separate "the budget was too short" (raise AFK_JUDGE_TIMEOUT) from "the
# judge is broken" (check the model/token).
#
# The timeout rc is NOT a fixed constant -- it depends on which bound _broker_run_bounded
# resolved, and that differs by CALLER CONTEXT:
#   * the danger-guard hook sources gate-broker.sh alone -> perl `alarm`/SIGALRM -> 142
#   * coreutils `timeout`, where installed                                        -> 124
#   * the SUPERVISOR sources hub-afk.sh too, so _broker_run_bounded prefers its
#     _afk_with_timeout, whose portable fallback tree-kills with TERM             -> 143
# Keying on the rc ALONE (the pre-#279 shape) therefore reported the #268 budget failure as
# "judge unavailable" in exactly the arm-time context #279 adds -- inverting the AC3 diagnostic
# at the one moment it pays off. So 124/142 stay hard-coded (unambiguous timeout codes), and any
# OTHER nonzero rc is a timeout only when the round trip actually ran out the budget. An
# operator's SIGTERM landing BEFORE the budget elapses still reads "unavailable", so the
# elapsed test never over-claims the way a bare `143 -> timed out` mapping would.
_judge_fail_reason() {
  local rc="$1" elapsed="${2:-0}" budget="${3:-0}"
  case "$rc" in
    124 | 142) printf 'judge timed out (rc=%s)' "$rc"; return ;;
  esac
  case "$elapsed$budget" in *[!0-9]*) elapsed=0; budget=0 ;; esac
  if [ "$budget" -gt 0 ] && [ "$elapsed" -ge "$budget" ]; then
    printf 'judge timed out (rc=%s)' "$rc"; return
  fi
  printf 'judge unavailable (rc=%s)' "$rc"
}

# judge_permission <cmd> [issue] -> "SAFE" or "DANGEROUS<TAB><reason>". Cache-first; otherwise
# run the toolless headless judge, bounded and fail-closed. Only a PARSED verdict (VERDICT:
# safe|dangerous) is cached: an unavailable or unparseable judge fails closed for THIS decision
# but is never cached, so a transient failure cannot poison the command for the whole window
# (#268). A nonzero rc also feeds the consecutive-unavailable streak (_judge_note_unavailable):
# at the threshold a drain-level halt is raised; a reachable judge clears it (_judge_note_available).
# Always rc 0 (the verdict is on stdout, like classify_permission / classify_danger).
judge_permission() {
  local cmd="$1" key cache f raw rc verdict tag cacheable=0 t0 elapsed
  key="$(_judge_cache_key "$cmd")"
  cache="$(_judge_cache_dir)"; f="$cache/$key"
  if [ -n "$key" ] && [ -f "$f" ]; then cat "$f" 2>/dev/null; return 0; fi
  t0="$(_judge_elapsed_now)"
  raw="$(_judge_raw "$cmd")"; rc=$?
  elapsed=$(( $(_judge_elapsed_now) - t0 ))
  if [ "$rc" -ne 0 ]; then
    verdict="$(printf 'DANGEROUS\t%s -- fail-closed' \
      "$(_judge_fail_reason "$rc" "$elapsed" "$(_judge_timeout)")")"
    # #268 AC4: an unavailable judge is a transient outcome -- count the consecutive streak and,
    # at the threshold, raise the drain-level halt so dispatch pauses instead of grinding on.
    _judge_note_unavailable
  else
    tag="$(_judge_parse_tag "$raw")"
    case "$tag" in
      safe) verdict="SAFE"; cacheable=1 ;;
      dangerous) verdict="$(printf 'DANGEROUS\tjudge verdict: dangerous')"; cacheable=1 ;;
      *) verdict="$(printf 'DANGEROUS\tjudge verdict unparseable -- fail-closed')" ;;
    esac
    # #268 AC4: rc 0 means the judge is REACHABLE (even an unparseable answer proves the CLI ran)
    # -- clear the consecutive-unavailable streak + any raised halt so the drain resumes.
    _judge_note_available
  fi
  if [ -n "$key" ] && [ "$cacheable" -eq 1 ]; then
    mkdir -p "$cache" 2>/dev/null || true
    printf '%s\n' "$verdict" > "$f" 2>/dev/null || true
  fi
  printf '%s\n' "$verdict"
}

# --- arm-time judge liveness probe (issue #279) --------------------------------
# The /afk arm path checks its dependencies STATICALLY, so the #268 host -- where a 2s budget
# could not cover a `claude -p` cold start -- armed clean and then fail-closed every uncached
# tier-3 verdict for an hour, diagnosed only by autopsying a stranded spoke's judge-cache. This
# probe is the arm-time round trip that catches that class BEFORE a single spoke dispatches.

# _judge_sentinel -> the command the liveness probe asks the judge to classify. Benign and
# read-only: the probe cares whether the judge ANSWERS, so the sentinel must never be
# interesting enough for its verdict to matter.
_judge_sentinel() { printf '%s\n' "${AFK_JUDGE_SENTINEL:-git status --porcelain}"; }

# _judge_diag <raw> -> the judge's OWN first non-empty output line, for the probe's
# operator-facing reason. Without it every failure class collapses to a bare rc: an expired
# token, a mistyped AFK_JUDGE_MODEL, and a `claude` missing from PATH all print "rc=1", which
# is the autopsy #279 exists to replace with an answer. Capped so a chatty CLI cannot flood
# the arm log, and CR-stripped so a progress-spinner line stays one line.
_judge_diag() {
  printf '%s' "$1" | tr -d '\r' | grep -v '^[[:space:]]*$' | head -n 1 | cut -c1-120
}

# broker_judge_probe [sentinel] -> rc 0 + "AVAILABLE<TAB><verdict>" when the real judge answered
# with a PARSED verdict; rc 1 + "UNAVAILABLE<TAB><reason>" on a timeout, a broken judge, or an
# unparseable answer. Runs through _judge_raw, so it exercises the real prompt/command/budget --
# AFK_JUDGE_TIMEOUT=1 reproduces #268 here exactly as it did in production.
#
# Only PARSE-ability is required, never a specific verdict value: a judge answering "dangerous"
# for a read-only sentinel is odd but demonstrably alive, and asserting the value would turn LLM
# nondeterminism into a false arm refusal.
#
# SIDE-EFFECT-FREE BY CONSTRUCTION (#268): a probe is not a decision, so it writes NO verdict
# cache entry (a cached sentinel would be the very poisoning #268 taught us to avoid) and
# touches NEITHER _judge_note_unavailable NOR _judge_note_available -- the streak and the
# drain-level halt belong to real decisions only. A probe that counted its own failures would
# hand the fresh window a pre-raised halt; one that cleared the flag would resume dispatch on
# no evidence. That is why this calls _judge_raw directly rather than reusing judge_permission.
broker_judge_probe() {
  local cmd raw rc tag reason diag t0 elapsed
  cmd="${1:-$(_judge_sentinel)}"
  t0="$(_judge_elapsed_now)"
  raw="$(_judge_raw "$cmd" 1)"; rc=$?          # 1 = keep stderr: this is a diagnostic
  elapsed=$(( $(_judge_elapsed_now) - t0 ))
  if [ "$rc" -ne 0 ]; then
    reason="$(_judge_fail_reason "$rc" "$elapsed" "$(_judge_timeout)")"
  else
    tag="$(_judge_parse_tag "$raw")"
    if [ -n "$tag" ]; then
      printf 'AVAILABLE\tjudge verdict: %s\n' "$tag"
      return 0
    fi
    reason="judge verdict unparseable"
  fi
  # Append the judge's own words when it said anything -- the difference between "rc=1" and
  # "rc=1: Invalid API key - please run /login" is the whole value of an arm-time diagnostic.
  diag="$(_judge_diag "$raw")"
  if [ -n "$diag" ]; then
    printf 'UNAVAILABLE\t%s: %s\n' "$reason" "$diag"
  else
    printf 'UNAVAILABLE\t%s\n' "$reason"
  fi
  return 1
}
