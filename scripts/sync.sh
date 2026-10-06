#!/usr/bin/env bash
# ai-toolkit v2 sync: copy policy + lifecycle into a target repo. Claude Code only (D4): no per-platform
# projection, the frontmatter lives in the source files. Idempotent; files a previous run wrote and this
# run did not are removed (manifest GC). Usage: sync.sh <target-repo> [--no-commit] [--local-only] [--migrate-v1]
#   rules/*.md (guidelines too)    -> .claude/rules/ai-toolkit/ (no paths: = always on; paths: = conditional)
#   rules/on-demand/*.md           -> .ai-toolkit/rules/ (never auto-loaded). The project's own CLAUDE.md is never read, written or backed up.
#   skills/ agents/ prompts/       -> .claude/{skills,agents,commands}
#   {scripts,bin}, hooks/{git,claude}, ai-toolkit.env -> .ai-toolkit/   settings/claude/settings.json -> .ai-toolkit/claude-settings.json (bin/claude-spoke passes it with --settings)
#   The project's .claude/settings.json is never read or written; the hook scripts run from the main checkout's .ai-toolkit/hooks/claude, not from a worktree.
#   orca.yaml generated (setup/archive run from $ORCA_ROOT_PATH, where a new worktree has no .ai-toolkit)
# On a host (not the toolkit itself, not --local-only, not --no-commit) orca.yaml, the one tracked file, is committed alone and pushed to BASE_BRANCH; a refused push keeps
# the commit and the next run pushes it. Refused before anything is written: not on the base branch, not a fast-forward, a human's edit to orca.yaml, a linked worktree.
set -euo pipefail
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# owner_repo <git-url>: the owner/repo of an https, ssh or scp-style remote; fails on anything else (a local path is no repo to file to).
owner_repo() {
  local up
  up="$(printf '%s' "$1" | sed -E 's#^[a-z+]+://([^@/]+@)?[^/:]+/##; s#^[^@/:]+@[^/:]+:##; s#/$##; s#\.git$##')"
  [[ "$up" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] && printf '%s' "$up"
}
[ "${BASH_SOURCE[0]}" = "$0" ] || return 0   # sourced (the tests exercise owner_repo alone): define only

V2="$(cd "$AI_TOOLKIT_LIB_DIR/.." && pwd -P)"   # physical paths: the toolkit reached through a symlink is still the toolkit
SHARED="${AI_TOOLKIT_SHARED:-$V2/shared}"
[ -d "$SHARED" ] || SHARED="$V2/../shared"   # before cutover shared/ is one level above v2/
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE   # git's own answer about a checkout, never the environment's
TARGET="" LOCAL=0 MIGRATE=0 COMMIT=1
for a in "$@"; do
  case "$a" in --local-only) LOCAL=1 ;; --migrate-v1) MIGRATE=1 ;; --no-commit) COMMIT=0 ;; -*) die "unknown option: $a" ;; *) TARGET="$a" ;; esac
done
[ -n "$TARGET" ] || die "usage: sync.sh <target-repo> [--no-commit] [--local-only] [--migrate-v1]"
git -C "$TARGET" rev-parse --git-dir > /dev/null 2>&1 || die "$TARGET is not a git repository"
TARGET="$(cd "$TARGET" && pwd -P)"
if [ "$LOCAL" -eq 1 ] || [ "$TARGET" = "$V2" ]; then COMMIT=0; fi   # the toolkit's own checkout and a never-committed deployment have nothing to commit
OLD="$TARGET/.ai-toolkit/sync-manifest"
TRACKED="$(git -C "$TARGET" -c core.quotePath=false ls-files -- .claude .ai-toolkit CLAUDE.md)"
tracked() { [[ $'\n'$TRACKED$'\n' == *$'\n'"$1"$'\n'* ]]; }
orca_yaml() { cat << 'EOF'
setupAgentStartupPolicy: wait-for-setup
scripts:
  setup: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/setup.sh"
  archive: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/archive.sh"
EOF
}
tg() { git -C "$TARGET" "$@"; }
# linked <dir>: a linked worktree, by git's own answer (its git dir is not the common one), not by a path or a variable a worker controls. A push made inside this script
# is invisible to push-guard (it reads command text), so a worker must not reach the base branch through it: refused for the target and for the toolkit copy being run.
linked() { [ "$(git -C "$1" rev-parse --path-format=absolute --git-dir 2> /dev/null)" != "$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2> /dev/null)" ]; }
MSG="chore(ai-toolkit): sync orca.yaml with the toolkit (#0)"
if [ "$COMMIT" -eq 1 ]; then   # the refusals, before anything is written to the host
  if linked "$TARGET"; then die "$TARGET is a linked worktree: sync the main checkout (--no-commit writes the files without a commit)"; fi
  if linked "$V2"; then die "this toolkit copy ($V2) is in a linked worktree: run sync.sh from a main checkout (--no-commit writes without a commit)"; fi
  ORCA_ROOT_PATH="$TARGET" load_env; BASE="${BASE_BRANCH:-main}"
  [ "$(tg symbolic-ref -q --short HEAD || true)" = "$BASE" ] || die "$TARGET is not on its base branch $BASE: check it out, or set BASE_BRANCH in .ai-toolkit/ai-toolkit.local.env (--no-commit skips this)"
  GIT_TERMINAL_PROMPT=0 tg fetch -q origin "$BASE" 2> /dev/null || true   # offline is no refusal: the push will say so
  if tg rev-parse -q --verify "refs/remotes/origin/$BASE" > /dev/null && ! tg merge-base --is-ancestor "origin/$BASE" HEAD 2> /dev/null; then
    die "$BASE is behind or has diverged from origin/$BASE: bring it up to date first, the sync never pulls, rebases or forces (--no-commit skips this)"; fi
  if [ -n "$(tg status --porcelain --untracked-files=no -- orca.yaml)" ] && ! orca_yaml | cmp -s - "$TARGET/orca.yaml"; then
    die "orca.yaml has uncommitted changes the sync did not write: commit or discard them (git checkout HEAD -- orca.yaml), then run again (--no-commit overwrites it)"; fi
fi
NEW="$(mktemp)"; TMP="$(mktemp)"; PUT_TMP=""
trap 'rm -f "$NEW" "$TMP" "$PUT_TMP"' EXIT   # PUT_TMP: a put() cut short must not leave its half-written <dst>.new.<pid> behind

# put <src> <dst-rel> [bak]: copy when different (no mtime churn) and record it. bak = a singleton
# (orca.yaml): an existing file this tool never wrote is kept once as <dst>.bak.
# The copy is a rename, never an in-place write: land.sh syncs under a live coordinator loop, and bash reads its script by offset,
# so a truncate+write would corrupt the next line it reads. A renamed-over file leaves the loop's open inode complete (old code, consistent).
# The new file takes the destination's mode (cp -p of it, then its content) and a symlinked destination is written through, as an in-place cp did.
put() {
  local d="$TARGET/$2"
  case "$2" in .claude/*) if tracked "$2"; then grep -qxF "$2" "$OLD" 2> /dev/null || warn "$2 is tracked by the project: left alone"; return 0; fi ;; esac   # a .claude/ file the project tracks is its own; one a sync wrote is reported by drop()
  mkdir -p "$(dirname "$d")"
  if ! cmp -s "$1" "$d"; then
    if [ "${3:-}" = bak ] && [ -f "$d" ] && [ ! -e "$d.bak" ] && ! grep -qxF "$2" "$OLD" 2> /dev/null; then cp "$d" "$d.bak"; fi
    if [ -L "$d" ]; then cp "$1" "$d"
    else
      PUT_TMP="$d.new.$$"
      if [ -f "$d" ]; then cp -p "$d" "$PUT_TMP" && cat "$1" > "$PUT_TMP"; else cp "$1" "$PUT_TMP"; fi && mv -f "$PUT_TMP" "$d" || { rm -f "$PUT_TMP"; return 1; }
      PUT_TMP=""
    fi
  fi
  echo "$2" >> "$NEW"
}
put_md() { # put_md <src-dir> <dst-rel-dir>: the *.md files of one directory
  local f
  for f in "$1"/*.md; do if [ -f "$f" ]; then put "$f" "$2/${f##*/}"; fi; done
}
put_tree() { # put_tree <src-dir> <dst-rel-dir> [skip-name]: everything below it, minus caches
  local f
  [ -d "$1" ] || return 0
  while IFS= read -r f; do put "$1/$f" "$2/$f"; done < <(cd "$1" && find . -type f ! -name .DS_Store ! -name "${3:-.DS_Store}" ! -name '*.pyc' ! -path '*/__pycache__/*' | sed 's|^\./||' | LC_ALL=C sort)
}

put_md "$SHARED/rules" .claude/rules/ai-toolkit
put_md "$SHARED/rules/on-demand" .ai-toolkit/rules
put_tree "$SHARED/skills" .claude/skills
put_md "$SHARED/agents" .claude/agents
put_md "$SHARED/prompts" .claude/commands
put_tree "$V2/scripts" .ai-toolkit/scripts otel.sh   # the collector is per-machine: it runs from the toolkit checkout
put_tree "$V2/bin" .ai-toolkit/bin
put_tree "$V2/hooks/claude" .ai-toolkit/hooks/claude   # a worker runs the main checkout's copy (AI_TOOLKIT_DIR, set by the launcher), not one it could edit
put_tree "$V2/hooks/git" .ai-toolkit/hooks/git
# A host project files the toolkit's own defects to the toolkit's repo (UPSTREAM_REPO = its origin as owner/repo); the toolkit itself keeps it empty.
up=""
if [ "$TARGET" != "$V2" ]; then
  up="$(owner_repo "$(git -C "$V2" remote get-url origin 2> /dev/null)")" || { up=""; warn "the toolkit checkout has no usable origin: UPSTREAM_REPO left empty in $TARGET, so the scopers will draft, not file, a toolkit defect"; }
fi
sed "s|^UPSTREAM_REPO=.*|UPSTREAM_REPO=$up|" "$V2/settings/ai-toolkit.env" > "$TMP"
put "$TMP" .ai-toolkit/ai-toolkit.env
if [ -f "$V2/settings/claude/settings.json" ]; then put "$V2/settings/claude/settings.json" .ai-toolkit/claude-settings.json
else warn "$V2/settings/claude/settings.json is missing: .ai-toolkit/claude-settings.json (hooks) not synced"; fi
orca_yaml > "$TMP"
[ "$TARGET" = "$V2" ] || put "$TMP" orca.yaml bak   # the toolkit syncing into itself keeps its tracked ./scripts/setup.sh orca.yaml

# drop <rel>: remove one synced file and its now-empty parents. An absolute or climbing path is
# never touched (a manifest is data in the target, not trusted).
drop() {
  case "$1" in '' | /* | *..*) return 0 ;; esac
  if tracked "$1"; then warn "$1 is tracked in $TARGET, so it stays ($1.bak, if there, is the project's original): git rm it if it is the toolkit's"; return 0; fi
  case "$1" in CLAUDE.md | .claude/settings.json) if [ "$TARGET" != "$V2" ] && [ -f "$TARGET/$1.bak" ]; then mv -f "$TARGET/$1.bak" "$TARGET/$1"; return 0; fi ;; esac   # an earlier sync replaced the project's own (never in the toolkit itself: its .bak is a v1 sync's output)
  rm -f "$TARGET/$1"
  (cd "$TARGET" && rmdir -p "$(dirname "$1")" 2> /dev/null) || true
}
# GC: what the previous manifest lists and this run did not write.
if [ -f "$OLD" ]; then
  while IFS= read -r p; do
    grep -qxF "$p" "$NEW" || drop "$p"
  done < "$OLD"
fi
# A target a v1 sync filled (.cursor/, .github/, v1 .claude/ hooks): opt-in removal of what its manifest lists.
if [ -f "$TARGET/.ai-toolkit-manifest.json" ]; then
  if [ "$MIGRATE" -eq 1 ]; then
    jq -r '.tools[][]' "$TARGET/.ai-toolkit-manifest.json" > "$TMP" || die "unreadable .ai-toolkit-manifest.json"
    while IFS= read -r p; do
      grep -qxF "$p" "$NEW" || drop "$p"
    done < "$TMP"
    rm -f "$TARGET/.ai-toolkit-manifest.json"
  else
    warn "v1 manifest found: --migrate-v1 removes the Cursor/Copilot/v1 files it lists"
  fi
fi
mkdir -p "$TARGET/.ai-toolkit"
LC_ALL=C sort -u "$NEW" > "$TMP"
cmp -s "$TMP" "$OLD" || cp "$TMP" "$OLD"

# Per-clone ignores, as a marked block in .git/info/exclude (never the tracked .gitignore): .ai-toolkit/ (holds
# ai-toolkit.local.env) and every .claude/ path written, on every sync; --local-only also orca.yaml (written, never committed).
ex="$(git -C "$TARGET" rev-parse --path-format=absolute --git-path info/exclude)"
mkdir -p "$(dirname "$ex")"
{
  sed '/^# >>> ai-toolkit sync/,/^# <<< ai-toolkit sync/d' "$ex" 2> /dev/null || true
  echo '# >>> ai-toolkit sync'; echo '/.ai-toolkit/'
  grep '^\.claude/' "$NEW" | sed 's|^|/|' || true   # what this run wrote there, path by path: a file the project tracks or owns is never swept in
  if [ "$LOCAL" -eq 1 ]; then echo /orca.yaml; fi
  if [ -e "$TARGET/orca.yaml.bak" ]; then echo /orca.yaml.bak; fi   # the project's own, kept once: out of git status, never committed
  echo '# <<< ai-toolkit sync'
} > "$TMP"
cmp -s "$TMP" "$ex" || cp "$TMP" "$ex"
echo "synced $(wc -l < "$OLD" | tr -d ' ') files into $TARGET"
if [ "$COMMIT" -eq 1 ]; then
  if [ -n "$(tg status --porcelain -- orca.yaml)" ]; then   # changed or new: that file alone, whatever else the host has staged
    tg add -- orca.yaml && AI_TOOLKIT_ALLOW_BASE_COMMIT=1 tg commit -q -m "$MSG" -- orca.yaml || die "could not commit orca.yaml (a host hook? no git identity?): it is written, commit it yourself"
  fi
  if [ -z "$(tg status --porcelain -- orca.yaml)" ] && [ -z "$(tg ls-files -- orca.yaml)" ]; then warn "orca.yaml is ignored by git here (a global or project ignore rule): Orca reads the tracked one, so add it yourself (git add -f orca.yaml) and commit it"; fi
  range="origin/$BASE..HEAD"; tg rev-parse -q --verify "refs/remotes/origin/$BASE" > /dev/null || range=HEAD   # no remote branch yet: everything is waiting
  waiting="$(tg log --format=%s "$range" 2> /dev/null || true)"
  # Only the sync's own commits go out: its subject, orca.yaml alone, and the tip's orca.yaml byte for byte what this run writes (a subject alone is no proof)
  if [ -z "$waiting" ]; then :
  elif [ -n "$(printf '%s\n' "$waiting" | grep -vxF "$MSG" || true)$(tg log --format= --name-only "$range" | grep -v '^$' | grep -vxF orca.yaml || true)" ] || ! tg show HEAD:orca.yaml 2> /dev/null | cmp -s - <(orca_yaml); then
    warn "commits other than the sync's orca.yaml are waiting on $BASE: push them yourself (git -C $TARGET push origin HEAD:$BASE), the sync never publishes them"
  elif err="$(GIT_TERMINAL_PROMPT=0 tg push -q origin "HEAD:refs/heads/$BASE" 2>&1)"; then echo "committed and pushed orca.yaml to origin/$BASE"
  else warn "orca.yaml is committed locally, NOT pushed (${err%%$'\n'*}): fix the remote and run the sync again, or push it yourself: git -C $TARGET push origin HEAD:$BASE (a protected branch takes a pull request)"; fi
fi
