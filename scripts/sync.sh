#!/usr/bin/env bash
# ai-toolkit v2 sync: copy policy + lifecycle into a target repo. Claude Code only (D4): no per-platform
# projection, the frontmatter lives in the source files. Idempotent; files a previous run wrote and this
# run did not are removed (manifest GC). Usage: sync.sh <target-repo> [--local-only] [--migrate-v1]
#   rules/*.md (guidelines too)    -> .claude/rules/ai-toolkit/ (no paths: = always on; paths: = conditional)
#   rules/on-demand/*.md           -> .ai-toolkit/rules/ (never auto-loaded). The project's own CLAUDE.md is never read, written or backed up.
#   skills/ agents/ prompts/       -> .claude/{skills,agents,commands}
#   {scripts,bin} (under v2/ until the cutover), hooks/git, ai-toolkit.env -> .ai-toolkit/   hooks/claude -> .claude/hooks   settings/claude/settings.json -> .claude/settings.json
#   orca.yaml generated (setup/archive run from $ORCA_ROOT_PATH, where a new worktree has no .ai-toolkit)
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

V2="$(cd "$AI_TOOLKIT_LIB_DIR/.." && pwd)"
SHARED="${AI_TOOLKIT_SHARED:-$V2/shared}"
[ -d "$SHARED" ] || SHARED="$V2/../shared"   # before cutover shared/ is one level above v2/
TARGET="" LOCAL=0 MIGRATE=0
for a in "$@"; do
  case "$a" in --local-only) LOCAL=1 ;; --migrate-v1) MIGRATE=1 ;; -*) die "unknown option: $a" ;; *) TARGET="$a" ;; esac
done
[ -n "$TARGET" ] || die "usage: sync.sh <target-repo> [--local-only] [--migrate-v1]"
git -C "$TARGET" rev-parse --git-dir > /dev/null 2>&1 || die "$TARGET is not a git repository"
TARGET="$(cd "$TARGET" && pwd)"
OLD="$TARGET/.ai-toolkit/sync-manifest"
TRACKED="$(git -C "$TARGET" -c core.quotePath=false ls-files -- .claude .ai-toolkit CLAUDE.md)"
tracked() { [[ $'\n'$TRACKED$'\n' == *$'\n'"$1"$'\n'* ]]; }
NEW="$(mktemp)"; TMP="$(mktemp)"; PUT_TMP=""
trap 'rm -f "$NEW" "$TMP" "$PUT_TMP"' EXIT   # PUT_TMP: a put() cut short must not leave its half-written <dst>.new.<pid> behind

# put <src> <dst-rel> [bak]: copy when different (no mtime churn) and record it. bak = a singleton
# (settings.json, orca.yaml): an existing file this tool never wrote is kept once as <dst>.bak.
# The copy is a rename, never an in-place write: land.sh syncs under a live coordinator loop, and bash reads its script by offset,
# so a truncate+write would corrupt the next line it reads. A renamed-over file leaves the loop's open inode complete (old code, consistent).
# The new file takes the destination's mode (cp -p of it, then its content) and a symlinked destination is written through, as an in-place cp did.
put() {
  local d="$TARGET/$2"
  case "$2" in .claude/*) if tracked "$2"; then warn "$2 is tracked by the project: left alone"; return 0; fi ;; esac   # a .claude/ file the project tracks is its own
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
put_tree "$V2/hooks/claude" .claude/hooks   # setup.sh copies .claude/ and .ai-toolkit/rules/ into a new worktree
put_tree "$V2/hooks/git" .ai-toolkit/hooks/git
# A host project files the toolkit's own defects to the toolkit's repo (UPSTREAM_REPO = its origin as owner/repo); the toolkit itself keeps it empty.
up=""
if [ "$TARGET" != "$V2" ]; then
  up="$(owner_repo "$(git -C "$V2" remote get-url origin 2> /dev/null)")" || { up=""; warn "the toolkit checkout has no usable origin: UPSTREAM_REPO left empty in $TARGET, so the scopers will draft, not file, a toolkit defect"; }
fi
sed "s|^UPSTREAM_REPO=.*|UPSTREAM_REPO=$up|" "$V2/settings/ai-toolkit.env" > "$TMP"
put "$TMP" .ai-toolkit/ai-toolkit.env
if [ -f "$V2/settings/claude/settings.json" ]; then put "$V2/settings/claude/settings.json" .claude/settings.json bak
else warn "$V2/settings/claude/settings.json is missing: .claude/settings.json (hooks) not synced"; fi
cat > "$TMP" << 'EOF'
setupAgentStartupPolicy: wait-for-setup
scripts:
  setup: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/setup.sh"
  archive: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/archive.sh"
EOF
[ "$TARGET" = "$V2" ] || put "$TMP" orca.yaml bak   # the toolkit syncing into itself keeps its tracked ./scripts/setup.sh orca.yaml

# drop <rel>: remove one synced file and its now-empty parents. An absolute or climbing path is
# never touched (a manifest is data in the target, not trusted).
drop() {
  case "$1" in '' | /* | *..*) return 0 ;; esac
  if tracked "$1"; then warn "$1 is tracked in $TARGET, so it stays: git rm it if it is the toolkit's"; return 0; fi
  if [ "$1" = CLAUDE.md ] && [ -f "$TARGET/CLAUDE.md.bak" ]; then mv -f "$TARGET/CLAUDE.md.bak" "$TARGET/CLAUDE.md"; return 0; fi   # an earlier sync replaced the project's own
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
  echo '# <<< ai-toolkit sync'
} > "$TMP"
cmp -s "$TMP" "$ex" || cp "$TMP" "$ex"
echo "synced $(wc -l < "$OLD" | tr -d ' ') files into $TARGET"
