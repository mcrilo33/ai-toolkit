#!/usr/bin/env bash
# ai-toolkit v2 sync: copy policy + lifecycle into a target repo. Claude Code only (D4): no per-platform
# projection, the frontmatter lives in the source files. Idempotent; files a previous run wrote and this
# run did not are removed (manifest GC). Usage: sync.sh <target-repo> [--local-only] [--migrate-v1]
#   rules/*.md (minus guidelines)  -> .claude/rules/    (no paths: = always on; paths: = conditional)
#   rules/guidelines.md            -> CLAUDE.md         rules/on-demand/*.md -> .ai-toolkit/rules/ (never auto-loaded)
#   skills/ agents/ prompts/       -> .claude/{skills,agents,commands}
#   {scripts,bin} (under v2/ until the cutover), hooks/git, ai-toolkit.env -> .ai-toolkit/   hooks/claude -> .claude/hooks   settings/claude/settings.json -> .claude/settings.json
#   orca.yaml generated (setup/archive run from $ORCA_ROOT_PATH, where a new worktree has no .ai-toolkit)
set -euo pipefail
# shellcheck source=lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

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
NEW="$(mktemp)"; TMP="$(mktemp)"
trap 'rm -f "$NEW" "$TMP"' EXIT

# put <src> <dst-rel> [bak]: copy when different (no mtime churn) and record it. bak = a singleton
# (CLAUDE.md, settings.json, orca.yaml): an existing file this tool never wrote is kept once as <dst>.bak.
# The copy is a rename, never an in-place write: land.sh syncs under a live coordinator loop, and bash reads its script by offset,
# so a truncate+write would corrupt the next line it reads. A renamed-over file leaves the loop's open inode complete (old code, consistent).
put() {
  local d="$TARGET/$2"
  mkdir -p "$(dirname "$d")"
  if ! cmp -s "$1" "$d"; then
    if [ "${3:-}" = bak ] && [ -f "$d" ] && [ ! -e "$d.bak" ] && ! grep -qxF "$2" "$OLD" 2> /dev/null; then cp "$d" "$d.bak"; fi
    cp "$1" "$d.new.$$" && mv -f "$d.new.$$" "$d" || { rm -f "$d.new.$$"; return 1; }
  fi
  echo "$2" >> "$NEW"
}
put_md() { # put_md <src-dir> <dst-rel-dir> [skip-name]: the *.md files of one directory
  local f
  for f in "$1"/*.md; do
    [ -f "$f" ] || continue
    [ "${f##*/}" = "${3:-}" ] || put "$f" "$2/${f##*/}"
  done
}
put_tree() { # put_tree <src-dir> <dst-rel-dir> [skip-name]: everything below it, minus caches
  local f
  [ -d "$1" ] || return 0
  while IFS= read -r f; do put "$1/$f" "$2/$f"; done < <(cd "$1" && find . -type f ! -name .DS_Store ! -name "${3:-.DS_Store}" ! -name '*.pyc' ! -path '*/__pycache__/*' | sed 's|^\./||' | LC_ALL=C sort)
}

put_md "$SHARED/rules" .claude/rules guidelines.md
put_md "$SHARED/rules/on-demand" .ai-toolkit/rules
awk 'NR == 1 && /^---$/ { fm = 1; next } fm && /^---$/ { fm = 0; next } !fm' "$SHARED/rules/guidelines.md" > "$TMP"
put "$TMP" CLAUDE.md bak   # the frontmatter is for the lint, not for CLAUDE.md
put_tree "$SHARED/skills" .claude/skills
put_md "$SHARED/agents" .claude/agents
put_md "$SHARED/prompts" .claude/commands
put_tree "$V2/scripts" .ai-toolkit/scripts otel.sh   # the collector is per-machine: it runs from the toolkit checkout
put_tree "$V2/bin" .ai-toolkit/bin
put_tree "$V2/hooks/claude" .claude/hooks   # setup.sh copies only .claude/ into a new worktree
put_tree "$V2/hooks/git" .ai-toolkit/hooks/git
put "$V2/settings/ai-toolkit.env" .ai-toolkit/ai-toolkit.env
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

# Per-clone ignores, as a marked block in .git/info/exclude (never the tracked .gitignore):
# .ai-toolkit/ always (holds ai-toolkit.local.env); --local-only also the deployment files.
ex="$(git -C "$TARGET" rev-parse --path-format=absolute --git-path info/exclude)"
mkdir -p "$(dirname "$ex")"
{
  sed '/^# >>> ai-toolkit sync/,/^# <<< ai-toolkit sync/d' "$ex" 2> /dev/null || true
  echo '# >>> ai-toolkit sync'; echo '/.ai-toolkit/'
  if [ "$LOCAL" -eq 1 ]; then printf '/.claude/\n/CLAUDE.md\n/orca.yaml\n'; fi
  echo '# <<< ai-toolkit sync'
} > "$TMP"
cmp -s "$TMP" "$ex" || cp "$TMP" "$ex"
echo "synced $(wc -l < "$OLD" | tr -d ' ') files into $TARGET"
