#!/usr/bin/env bash
# build-cursor-plugin.sh — Package shared/ into a distributable Cursor plugin.
# Additive artifact: existing sync-to-repo.sh outputs are untouched.
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
    echo "Usage: $0 [output-dir]"
    echo ""
    echo "Builds the ai-toolkit Cursor plugin (default output: dist/cursor-plugin)"
    echo ""
    echo "Examples:"
    echo "  $0                      # Build into dist/cursor-plugin"
    echo "  $0 /tmp/my-plugin       # Build into a custom directory"
    exit 1
}

case "${1:-}" in
    -h|--help) usage ;;
esac

OUT="${1:-$REPO_DIR/dist/cursor-plugin}"
VERSION_FILE="$REPO_DIR/VERSION"

[ -f "$VERSION_FILE" ] || { error "VERSION file not found: $VERSION_FILE"; exit 1; }
VERSION="$(head -n1 "$VERSION_FILE" | tr -d '[:space:]')"
[ -n "$VERSION" ] || { error "VERSION file is empty"; exit 1; }

# ─── Python helper (same contract as sync-to-repo.sh) ───
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

# ─── Helper: copy skill subdirectories (references, scripts, templates, assets) ───
copy_skill_subdirs() {
    local src_dir="$1" dst_dir="$2"
    for subdir in references scripts templates assets; do
        [ -d "$src_dir/$subdir" ] || continue
        cp -R "$src_dir/$subdir" "$dst_dir/"
        info "  └── $subdir/"
    done
}

# ─── Field sets (must match sync-to-repo.sh cursor emission) ───
CURSOR_RULE_FIELDS="description,globs,alwaysApply"
CURSOR_SKILL_FIELDS="name,description,license,compatibility,metadata,disable-model-invocation"
CURSOR_AGENT_FIELDS="description,model,readonly,is_background"

# ═══════════════════════════════════════════
#  MANIFEST
# ═══════════════════════════════════════════
build_manifest() {
    section "Manifest"
    mkdir -p "$OUT/.cursor-plugin"
    python3 - "$VERSION" > "$OUT/.cursor-plugin/plugin.json" <<'PYEOF'
import json
import sys

plugin = {
    "name": "ai-toolkit",
    "description": (
        "Shared rules, skills, agents, and hooks for AI coding assistants — "
        "workflow gates, code quality, and security guardrails."
    ),
    "version": sys.argv[1],
    "author": {"name": "Mathieu Crilout"},
    "license": "MIT",
    "keywords": ["rules", "skills", "agents", "hooks", "workflow"],
}
print(json.dumps(plugin, indent=2))
PYEOF
    info ".cursor-plugin/plugin.json (version $VERSION)"
}

# ═══════════════════════════════════════════
#  RULES
# ═══════════════════════════════════════════
build_rules() {
    section "Rules"
    mkdir -p "$OUT/rules"
    query_metadata "$SHARED_DIR/rules/metadata.yml" cursor "$CURSOR_RULE_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/rules/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/rules/${name}.md" "$OUT/rules/${name}.mdc" "$fm"
        info "rules/${name}.mdc"
    done
}

# ═══════════════════════════════════════════
#  SKILLS
# ═══════════════════════════════════════════
build_skills() {
    section "Skills"
    mkdir -p "$OUT/skills"
    query_metadata "$SHARED_DIR/skills/metadata.yml" cursor "$CURSOR_SKILL_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/skills/${name}/SKILL.md" ] || continue
        mkdir -p "$OUT/skills/$name"
        add_frontmatter "$SHARED_DIR/skills/${name}/SKILL.md" "$OUT/skills/$name/SKILL.md" "$fm"
        info "skills/$name/"
    done
    # plain-copy skills without metadata entry
    for d in "$SHARED_DIR/skills/"*/; do
        [ -d "$d" ] || continue
        local s
        s="$(basename "$d")"
        [ -f "$d/SKILL.md" ] || continue
        if [ ! -f "$OUT/skills/$s/SKILL.md" ]; then
            mkdir -p "$OUT/skills/$s"; cp "$d/SKILL.md" "$OUT/skills/$s/SKILL.md"
            info "skills/$s/ (plain)"
        fi
        copy_skill_subdirs "$d" "$OUT/skills/$s"
    done
}

# ═══════════════════════════════════════════
#  AGENTS
# ═══════════════════════════════════════════
build_agents() {
    section "Agents"
    mkdir -p "$OUT/agents"
    query_metadata "$SHARED_DIR/agents/metadata.yml" cursor "$CURSOR_AGENT_FIELDS" | while IFS=$'\t' read -r name fm; do
        [ -f "$SHARED_DIR/agents/${name}.md" ] || continue
        add_frontmatter "$SHARED_DIR/agents/${name}.md" "$OUT/agents/${name}.md" "$fm"
        info "agents/${name}.md"
    done
    # plain-copy agents without metadata entry
    for f in "$SHARED_DIR/agents/"*.md; do
        [ -f "$f" ] || continue
        local a
        a="$(basename "$f" .md)"
        [ "$a" = "metadata" ] && continue
        if [ ! -f "$OUT/agents/${a}.md" ]; then
            cp "$f" "$OUT/agents/${a}.md"
            info "agents/${a}.md (plain)"
        fi
    done
}

# ═══════════════════════════════════════════
#  HOOKS
# ═══════════════════════════════════════════
build_hooks() {
    [ -f "$SHARED_DIR/hooks/metadata.yml" ] || return 0
    section "Hooks"
    mkdir -p "$OUT/hooks" "$OUT/scripts"

    for f in "$SHARED_DIR/hooks/"*.sh; do
        [ -f "$f" ] || continue
        cp "$f" "$OUT/scripts/"
        chmod +x "$OUT/scripts/$(basename "$f")"
        info "scripts/$(basename "$f")"
    done

    if [ -d "$SHARED_DIR/hooks/lib" ]; then
        mkdir -p "$OUT/scripts/lib"
        cp -R "$SHARED_DIR/hooks/lib/"* "$OUT/scripts/lib/" 2>/dev/null || true
        info "scripts/lib/"
    fi

    # Plugin hooks reference scripts relative to the plugin root (./scripts/<hook>.sh)
    python3 "$SCRIPT_DIR/hooks_generator.py" "$SHARED_DIR/hooks" "$OUT" cursor \
        --script-prefix=./scripts > "$OUT/hooks/hooks.json"
    info "hooks/hooks.json"
}

# ═══════════════════════════════════════════
#  README
# ═══════════════════════════════════════════
build_readme() {
    section "README"
    cat > "$OUT/README.md" <<EOF
# ai-toolkit — Cursor plugin

Shared rules, skills, agents, and hooks for AI coding assistants, packaged as a
Cursor plugin (version $VERSION).

## Contents

- \`rules/\` — instruction rules (guidelines, code quality, security, …)
- \`skills/\` — skills with \`SKILL.md\` entry points and supporting files
- \`agents/\` — specialist agent mode definitions
- \`hooks/hooks.json\` — lifecycle hooks wired to \`scripts/*.sh\`
- \`scripts/\` — hook scripts and shared utilities

Built from [ai-toolkit](https://github.com/MathieuCrilout/ai-toolkit) by
\`scripts/build-cursor-plugin.sh\`. Do not edit by hand — changes belong in the
source repo's \`shared/\` directory.
EOF
    info "README.md"
}

# ═══════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ai-toolkit — Build Cursor Plugin       ║"
echo "╚══════════════════════════════════════════╝"

# Clean rebuild: dist/ artifact, deterministic output.
# OUT derives from a user-supplied argument — refuse catastrophic targets
# before the recursive delete.
RESOLVED_OUT="$OUT"
[ -d "$OUT" ] && RESOLVED_OUT="$(cd "$OUT" && pwd)"
case "$RESOLVED_OUT" in
    /|"$HOME")
        error "Refusing to delete '$RESOLVED_OUT'"; exit 1 ;;
    *cursor-plugin) ;;
    *)
        error "Refusing to delete '$RESOLVED_OUT' — output dir must end in 'cursor-plugin'"; exit 1 ;;
esac
rm -rf "$OUT"
mkdir -p "$OUT"

build_manifest
build_rules
build_skills
build_agents
build_hooks
build_readme

echo ""
info "Plugin built: $OUT"
