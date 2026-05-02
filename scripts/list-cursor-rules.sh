#!/usr/bin/env bash
# List all Cursor rules available on disk with their load mode.
# Scans:
#   1. Global rules:    ~/.cursor/rules/*.mdc
#   2. Workspace rules: .cursor/rules/*.mdc  (relative to repo root)
#   3. Root-level:      CLAUDE.md, .cursorrules, AGENTS.md
#
# Usage: scripts/list-cursor-rules.sh [--workspace-dir <path>]

set -euo pipefail

WORKSPACE_DIR="."
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace-dir) WORKSPACE_DIR="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--workspace-dir <path>]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

GLOBAL_DIR="$HOME/.cursor/rules"
WS_DIR="$WORKSPACE_DIR/.cursor/rules"

parse_frontmatter() {
  local file="$1"
  local description="" globs="" always_apply=""

  while IFS= read -r line; do
    [[ "$line" == "---" ]] && continue
    case "$line" in
      description:*)  description="${line#description: }" ;;
      globs:*)        globs="${line#globs: }" ; globs="${globs//\"/}" ; globs="${globs## }" ;;
      alwaysApply:*)  always_apply="${line#alwaysApply: }" ;;
    esac
  done < <(awk '/^---$/{n++; next} n>=2{exit} {print}' "$file")

  local mode="agent-requestable"
  if [[ "$always_apply" == "true" ]]; then
    mode="always"
  elif [[ -n "$globs" && "$globs" != "globs:" ]]; then
    mode="on-demand (globs: $globs)"
  fi

  echo "$mode|$description"
}

header_printed=false
print_header() {
  if ! $header_printed; then
    printf "%-12s  %-35s  %-45s  %s\n" "SOURCE" "RULE" "MODE" "DESCRIPTION"
    printf "%-12s  %-35s  %-45s  %s\n" "──────" "────" "────" "───────────"
    header_printed=true
  fi
}

count=0

# Root-level auto-loaded files
for name in CLAUDE.md .cursorrules AGENTS.md; do
  target="$WORKSPACE_DIR/$name"
  if [[ -f "$target" ]]; then
    print_header
    printf "%-12s  %-35s  %-45s  %s\n" "workspace" "$name" "always (root file)" "-"
    ((count++))
  fi
done

# Global rules
if [[ -d "$GLOBAL_DIR" ]]; then
  for f in "$GLOBAL_DIR"/*.mdc; do
    [[ -f "$f" ]] || continue
    print_header
    base="$(basename "$f")"
    result="$(parse_frontmatter "$f")"
    mode="${result%%|*}"
    desc="${result#*|}"
    printf "%-12s  %-35s  %-45s  %s\n" "global" "$base" "$mode" "$desc"
    ((count++))
  done
fi

# Workspace rules
warn_count=0
if [[ -d "$WS_DIR" ]]; then
  for f in "$WS_DIR"/*.mdc; do
    [[ -f "$f" ]] || continue
    print_header
    base="$(basename "$f")"
    result="$(parse_frontmatter "$f")"
    mode="${result%%|*}"
    desc="${result#*|}"
    printf "%-12s  %-35s  %-45s  %s\n" "workspace" "$base" "$mode" "$desc"
    ((count++))
  done

  # Detect .md files that Cursor ignores (must be .mdc)
  for f in "$WS_DIR"/*.md; do
    [[ -f "$f" ]] || continue
    base="$(basename "$f")"
    # Skip if a .mdc counterpart already exists
    [[ -f "$WS_DIR/${base%.md}.mdc" ]] && continue
    print_header
    printf "%-12s  %-35s  %-45s  %s\n" "workspace" "$base" "⚠ IGNORED (.md, not .mdc)" "Rename to .mdc for Cursor to load it"
    ((warn_count++))
  done
fi

echo ""
echo "Total: $count rule(s) loaded"
if [[ $warn_count -gt 0 ]]; then
  echo "Warnings: $warn_count file(s) ignored — Cursor only loads .mdc files from .cursor/rules/"
fi
