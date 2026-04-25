#!/usr/bin/env bash
# sync-to-repo.sh — Generate tool-specific configs from shared/ into a target repo.
# Reads per-directory metadata.yml for frontmatter. Python 3 stdlib only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SHARED_DIR="$REPO_DIR/shared"

GREEN='\033[0;32m'  YELLOW='\033[1;33m'  RED='\033[0;31m'  BLUE='\033[0;34m'  NC='\033[0m'
info()    { echo -e "${GREEN}✓${NC} $1"; }
warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
error()   { echo -e "${RED}✗${NC} $1" >&2; }
section() { echo -e "\n${BLUE}── $1 ──${NC}"; }

usage() {
    echo "Usage: $0 <target-repo-path> [tool]"
    echo ""
    echo "Tools: copilot, cursor, claude, all (default: all)"
    echo ""
    echo "Examples:"
    echo "  $0 ~/Repos/my-project              # Sync all tools"
    echo "  $0 ~/Repos/my-project copilot       # Copilot only"
    echo "  $0 ~/Repos/my-project cursor        # Cursor only"
    echo "  $0 ~/Repos/my-project claude        # Claude only"
    exit 1
}

[ $# -lt 1 ] && usage
TARGET="$1"
TOOL="${2:-all}"
[ ! -d "$TARGET" ] && { error "Target directory does not exist: $TARGET"; exit 1; }

if [ ! -d "$TARGET/.git" ]; then
    warn "Target does not appear to be a git repository: $TARGET"
    read -rp "Continue anyway? [y/N] " confirm
    [[ ! "$confirm" =~ ^[Yy]$ ]] && { echo "Aborted."; exit 0; }
fi

# ─── Python helper ───
# Reads metadata.yml, merges shared defaults with per-tool overrides,
# emits only the requested fields.
# Args: $1=metadata.yml  $2=tool  $3=comma-separated fields
# Output: "name<TAB>field1: val\nfield2: val" per item
query_metadata() {
    local meta_file="$1" tool="$2" fields="$3"
    [ -f "$meta_file" ] || return 0
    python3 "$SCRIPT_DIR/metadata_parser.py" "$meta_file" "$tool" "$fields"
}

# ─── Helper: write file with YAML frontmatter ───
add_frontmatter() {
    local src="$1" dst="$2" meta="$3"
    { echo "---"; echo -e "$meta"; echo "---"; echo ""; cat "$src"; } > "$dst"
}

# ─── Field sets per tool per category ───
COPILOT_RULE_FIELDS="name,description,applyTo,excludeAgent"
CURSOR_RULE_FIELDS="description,globs,alwaysApply"
CLAUDE_RULE_FIELDS="paths"

COPILOT_SKILL_FIELDS="name,description"
CURSOR_SKILL_FIELDS="name,description"
CLAUDE_SKILL_FIELDS="name,description"

COPILOT_PROMPT_FIELDS="name,description,agent"
CLAUDE_PROMPT_FIELDS="name,description"

# ═══════════════════════════════════════════
#  COPILOT
# ═══════════════════════════════════════════
sync_copilot() {
    local gh="$TARGET/.github"
    section "Copilot → $gh/"
    mkdir -p "$gh/instructions" "$gh/skills" "$gh/agents" "$gh/prompts"

    # copilot-instructions.md ← guidelines.md
    cp "$SHARED_DIR/rules/guidelines.md" "$gh/copilot-instructions.md"
    info "copilot-instructions.md (from guidelines.md)"

    # instructions/*.instructions.md
    query_metadata "$SHARED_DIR/rules/metadata.yml" copilot "$COPILOT_RULE_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/rules/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/rules/${name}.md" "$gh/instructions/${name}.instructions.md" "$fm"
        info "instructions/${name}.instructions.md"
    done

    # skills/
    query_metadata "$SHARED_DIR/skills/metadata.yml" copilot "$COPILOT_SKILL_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/skills/${name}/SKILL.md" ] || continue
        mkdir -p "$gh/skills/$name"
        add_frontmatter "$SHARED_DIR/skills/${name}/SKILL.md" "$gh/skills/$name/SKILL.md" "$fm"
        info "skills/$name/"
    done
    # plain-copy skills without metadata entry
    for d in "$SHARED_DIR/skills/"*/; do
        [ -d "$d" ] || continue
        local s="$(basename "$d")"
        [ -f "$gh/skills/$s/SKILL.md" ] && continue
        [ -f "$d/SKILL.md" ] || continue
        mkdir -p "$gh/skills/$s"; cp "$d/SKILL.md" "$gh/skills/$s/SKILL.md"
        info "skills/$s/ (plain)"
    done

    # agents/
    for f in "$SHARED_DIR/agents/"*.agent.md; do [ -f "$f" ] && cp "$f" "$gh/agents/"; done
    info "agents/"

    # prompts/
    query_metadata "$SHARED_DIR/prompts/metadata.yml" copilot "$COPILOT_PROMPT_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/prompts/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/prompts/${name}.md" "$gh/prompts/${name}.prompt.md" "$fm"
        info "prompts/${name}.prompt.md"
    done
}

# ═══════════════════════════════════════════
#  CURSOR
# ═══════════════════════════════════════════
sync_cursor() {
    local cur="$TARGET/.cursor"
    section "Cursor → $cur/"
    mkdir -p "$cur/rules" "$cur/skills"

    # rules/*.md (or .mdc)
    query_metadata "$SHARED_DIR/rules/metadata.yml" cursor "$CURSOR_RULE_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/rules/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/rules/${name}.md" "$cur/rules/${name}.md" "$fm"
        info "rules/${name}.md"
    done

    # skills/
    query_metadata "$SHARED_DIR/skills/metadata.yml" cursor "$CURSOR_SKILL_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/skills/${name}/SKILL.md" ] || continue
        mkdir -p "$cur/skills/$name"
        add_frontmatter "$SHARED_DIR/skills/${name}/SKILL.md" "$cur/skills/$name/SKILL.md" "$fm"
        info "skills/$name/"
    done
    for d in "$SHARED_DIR/skills/"*/; do
        [ -d "$d" ] || continue
        local s="$(basename "$d")"
        [ -f "$cur/skills/$s/SKILL.md" ] && continue
        [ -f "$d/SKILL.md" ] || continue
        mkdir -p "$cur/skills/$s"; cp "$d/SKILL.md" "$cur/skills/$s/SKILL.md"
        info "skills/$s/ (plain)"
    done
}

# ═══════════════════════════════════════════
#  CLAUDE
# ═══════════════════════════════════════════
sync_claude() {
    local cl="$TARGET/.claude"
    section "Claude → $cl/"
    mkdir -p "$cl/rules" "$cl/skills"

    # CLAUDE.md ← guidelines.md
    cp "$SHARED_DIR/rules/guidelines.md" "$TARGET/CLAUDE.md"
    info "CLAUDE.md (from guidelines.md)"

    # rules/*.md (with paths)
    query_metadata "$SHARED_DIR/rules/metadata.yml" claude "$CLAUDE_RULE_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/rules/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/rules/${name}.md" "$cl/rules/${name}.md" "$fm"
        info "rules/${name}.md"
    done

    # skills/
    query_metadata "$SHARED_DIR/skills/metadata.yml" claude "$CLAUDE_SKILL_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/skills/${name}/SKILL.md" ] || continue
        mkdir -p "$cl/skills/$name"
        add_frontmatter "$SHARED_DIR/skills/${name}/SKILL.md" "$cl/skills/$name/SKILL.md" "$fm"
        info "skills/$name/"
    done
    for d in "$SHARED_DIR/skills/"*/; do
        [ -d "$d" ] || continue
        local s="$(basename "$d")"
        [ -f "$cl/skills/$s/SKILL.md" ] && continue
        [ -f "$d/SKILL.md" ] || continue
        mkdir -p "$cl/skills/$s"; cp "$d/SKILL.md" "$cl/skills/$s/SKILL.md"
        info "skills/$s/ (plain)"
    done

    # prompts/
    query_metadata "$SHARED_DIR/prompts/metadata.yml" claude "$CLAUDE_PROMPT_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/prompts/${name}.md" ] || continue
        mkdir -p "$cl/prompts"
        add_frontmatter "$SHARED_DIR/prompts/${name}.md" "$cl/prompts/${name}.md" "$fm"
        info "prompts/${name}.md"
    done
}

# ═══════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ai-toolkit — Sync to Repo              ║"
echo "╚══════════════════════════════════════════╝"

case "$TOOL" in
    copilot) sync_copilot ;;
    cursor)  sync_cursor ;;
    claude)  sync_claude ;;
    all)     sync_copilot; sync_cursor; sync_claude ;;
    *)       error "Unknown tool: $TOOL"; usage ;;
esac

echo ""
info "Sync complete! Review changes with: cd $TARGET && git diff"
