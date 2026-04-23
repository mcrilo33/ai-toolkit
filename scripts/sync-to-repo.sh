#!/usr/bin/env bash
# sync-to-repo.sh — Copy copilot/ overlay into a target repo's .github/
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1" >&2; }

if [ $# -lt 1 ]; then
    error "Usage: $0 <target-repo-path>"
    echo "  Example: $0 ~/Repos/my-project"
    exit 1
fi

TARGET="$1"

if [ ! -d "$TARGET" ]; then
    error "Target directory does not exist: $TARGET"
    exit 1
fi

if [ ! -d "$TARGET/.git" ]; then
    warn "Target does not appear to be a git repository: $TARGET"
    read -rp "Continue anyway? [y/N] " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

GITHUB_DIR="$TARGET/.github"
mkdir -p "$GITHUB_DIR"

echo ""
echo "Syncing copilot/ → $GITHUB_DIR/"
echo ""

# Copy copilot-instructions.md
if [ -f "$REPO_DIR/copilot/copilot-instructions.md" ]; then
    cp "$REPO_DIR/copilot/copilot-instructions.md" "$GITHUB_DIR/copilot-instructions.md"
    info "copilot-instructions.md"
fi

# Copy instructions/
if [ -d "$REPO_DIR/copilot/instructions" ]; then
    mkdir -p "$GITHUB_DIR/instructions"
    cp "$REPO_DIR/copilot/instructions/"*.instructions.md "$GITHUB_DIR/instructions/" 2>/dev/null || true
    info "instructions/"
fi

# Copy skills/
if [ -d "$REPO_DIR/copilot/skills" ]; then
    # Use rsync to copy directory structure
    rsync -a --delete "$REPO_DIR/copilot/skills/" "$GITHUB_DIR/skills/"
    info "skills/"
fi

# Copy agents/
if [ -d "$REPO_DIR/copilot/agents" ]; then
    mkdir -p "$GITHUB_DIR/agents"
    for f in "$REPO_DIR/copilot/agents/"*.agent.md; do
        [ -f "$f" ] && cp "$f" "$GITHUB_DIR/agents/"
    done
    info "agents/"
fi

# Copy prompts/
if [ -d "$REPO_DIR/copilot/prompts" ]; then
    mkdir -p "$GITHUB_DIR/prompts"
    for f in "$REPO_DIR/copilot/prompts/"*.prompt.md; do
        [ -f "$f" ] && cp "$f" "$GITHUB_DIR/prompts/"
    done
    info "prompts/"
fi

echo ""
info "Sync complete: $GITHUB_DIR/"
echo "  Review changes with: cd $TARGET && git diff"
