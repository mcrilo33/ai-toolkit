#!/usr/bin/env bash
# diff-check.sh — Detect drift between shared/ and tool overlays
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; }

DRIFT_FOUND=0

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ai-toolkit — Drift Check               ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Compare shared/rules/ content against cursor/rules/ (stripping frontmatter)
strip_frontmatter() {
    # Remove YAML frontmatter (--- ... ---) from beginning of file
    awk '
    BEGIN { in_fm=0; seen_start=0 }
    /^---$/ && !seen_start { in_fm=1; seen_start=1; next }
    /^---$/ && in_fm { in_fm=0; next }
    !in_fm { print }
    ' "$1"
}

echo "── Shared Rules vs Cursor Rules ──"
for shared_file in "$REPO_DIR/shared/rules/"*.md; do
    [ -f "$shared_file" ] || continue
    name="$(basename "$shared_file" .md)"
    cursor_file="$REPO_DIR/cursor/rules/${name}.mdc"

    if [ ! -f "$cursor_file" ]; then
        warn "No cursor overlay for: shared/rules/$name.md"
        DRIFT_FOUND=1
        continue
    fi

    # Compare content (stripping frontmatter from cursor .mdc)
    shared_content="$(cat "$shared_file")"
    cursor_content="$(strip_frontmatter "$cursor_file")"

    if [ "$shared_content" = "$cursor_content" ]; then
        info "$name: in sync"
    else
        warn "$name: DRIFT DETECTED"
        echo "    diff shared/rules/$name.md cursor/rules/$name.mdc"
        DRIFT_FOUND=1
    fi
done

echo ""
echo "── Shared Rules vs Copilot Instructions ──"
for shared_file in "$REPO_DIR/shared/rules/"*.md; do
    [ -f "$shared_file" ] || continue
    name="$(basename "$shared_file" .md)"
    copilot_file="$REPO_DIR/copilot/instructions/${name}.instructions.md"

    if [ ! -f "$copilot_file" ]; then
        # Not all rules have copilot instructions counterparts
        echo "  ℹ  No copilot overlay for: $name (may be intentional)"
        continue
    fi

    shared_content="$(cat "$shared_file")"
    copilot_content="$(strip_frontmatter "$copilot_file")"

    if [ "$shared_content" = "$copilot_content" ]; then
        info "$name: in sync"
    else
        warn "$name: DRIFT DETECTED"
        echo "    diff shared/rules/$name.md copilot/instructions/$name.instructions.md"
        DRIFT_FOUND=1
    fi
done

echo ""
echo "── Shared Skills vs Cursor Skills ──"
for shared_skill_dir in "$REPO_DIR/shared/skills/"*/; do
    [ -d "$shared_skill_dir" ] || continue
    skill="$(basename "$shared_skill_dir")"
    shared_file="$shared_skill_dir/SKILL.md"
    cursor_file="$REPO_DIR/cursor/skills/$skill/SKILL.md"

    if [ ! -f "$cursor_file" ]; then
        warn "No cursor overlay for: shared/skills/$skill"
        DRIFT_FOUND=1
        continue
    fi

    shared_content="$(strip_frontmatter "$shared_file")"
    cursor_content="$(strip_frontmatter "$cursor_file")"

    if [ "$shared_content" = "$cursor_content" ]; then
        info "$skill: in sync"
    else
        warn "$skill: DRIFT DETECTED"
        DRIFT_FOUND=1
    fi
done

echo ""
echo "── Shared Skills vs Copilot Skills ──"
for shared_skill_dir in "$REPO_DIR/shared/skills/"*/; do
    [ -d "$shared_skill_dir" ] || continue
    skill="$(basename "$shared_skill_dir")"
    shared_file="$shared_skill_dir/SKILL.md"
    copilot_file="$REPO_DIR/copilot/skills/$skill/SKILL.md"

    if [ ! -f "$copilot_file" ]; then
        warn "No copilot overlay for: shared/skills/$skill"
        DRIFT_FOUND=1
        continue
    fi

    shared_content="$(strip_frontmatter "$shared_file")"
    copilot_content="$(strip_frontmatter "$copilot_file")"

    if [ "$shared_content" = "$copilot_content" ]; then
        info "$skill: in sync"
    else
        warn "$skill: DRIFT DETECTED"
        DRIFT_FOUND=1
    fi
done

echo ""
if [ "$DRIFT_FOUND" -eq 0 ]; then
    info "All overlays are in sync with shared/ ✨"
else
    warn "Drift detected — review the files listed above."
    echo "  Tip: Update overlays from shared/ or vice versa."
fi

exit $DRIFT_FOUND
