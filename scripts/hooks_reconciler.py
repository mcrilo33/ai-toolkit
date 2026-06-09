#!/usr/bin/env python3
"""Reconcile ai-toolkit-managed hooks into a target config file.

The sync pipeline treats every hook ai-toolkit emits as a *managed, owned* set.
Naive appends bloat downstream configs with duplicate entries on every sync; a
reconcile converges to a fixed point instead:

  1. Remove all existing ai-toolkit-owned entries from each event bucket.
  2. Append the freshly generated ai-toolkit hook set exactly once.
  3. Preserve any entry that is NOT ai-toolkit-owned (user/custom hooks),
     untouched and in place.
  4. De-duplicate the final bucket by canonical key as a safety net.

This makes repeated syncs idempotent and self-heals already-bloated files,
while never disturbing user-authored hooks.

Ownership rule: an entry is ai-toolkit-owned iff any command string it carries
points into the managed ``hooks/scripts/`` path (the location sync-to-repo.sh
copies the shared cage scripts into for every platform).

Usage:
    python3 hooks_reconciler.py <kind> <existing-file-or-empty> < generated.json

  <kind> is one of:
    cursor   — config shape: {"version": 1, "hooks": {EVENT: [entry, ...]}}
    claude   — config shape: {..., "hooks": {EVENT: [group, ...]}}  (settings.json)

Reads the freshly generated ai-toolkit JSON on stdin, reads the existing target
file (path may be empty/non-existent), writes the reconciled full file to stdout.

Can also be imported as a module for unit testing.
"""

from __future__ import annotations

import json
import sys

# Marker present in every ai-toolkit-emitted hook command, across all three
# platforms (./.github/hooks/scripts/, ./.cursor/hooks/scripts/,
# "$CLAUDE_PROJECT_DIR"/.claude/hooks/scripts/).
OWNED_MARKER = "hooks/scripts/"


def _canonical_key(entry: dict) -> str:
    """Stable identity for an entry, used for dedup."""
    return json.dumps(entry, sort_keys=True)


def _entry_commands(entry: dict) -> list[str]:
    """Collect every command string an entry carries.

    Cursor/Copilot entries hold the command directly (``command`` / ``bash``).
    Claude entries are matcher *groups* that nest handlers under ``hooks``,
    each handler carrying a ``command``.
    """
    commands: list[str] = []
    for field in ("command", "bash"):
        val = entry.get(field)
        if isinstance(val, str):
            commands.append(val)
    nested = entry.get("hooks")
    if isinstance(nested, list):
        for handler in nested:
            if isinstance(handler, dict):
                commands.extend(_entry_commands(handler))
    return commands


def _is_owned(entry: dict) -> bool:
    """True if any command in the entry points into the managed scripts path."""
    return any(OWNED_MARKER in cmd for cmd in _entry_commands(entry))


def reconcile_bucket(existing: list[dict], generated: list[dict]) -> list[dict]:
    """Reconcile a single event bucket.

    Keeps non-owned entries in their original order, drops pre-existing
    ai-toolkit-owned entries, then appends the freshly generated set once.
    De-duplicates by canonical key as a final safety net.
    """
    result: list[dict] = [e for e in existing if not _is_owned(e)]
    result.extend(generated)

    seen: set[str] = set()
    deduped: list[dict] = []
    for entry in result:
        key = _canonical_key(entry)
        if key not in seen:
            seen.add(key)
            deduped.append(entry)
    return deduped


def reconcile_cursor(existing: dict, generated: dict) -> dict:
    """Reconcile a Cursor hooks.json document.

    Shape: ``{"version": 1, "hooks": {EVENT: [entry, ...]}}``.
    """
    result = dict(existing)
    existing_hooks: dict = dict(existing.get("hooks", {}) or {})
    generated_hooks: dict = generated.get("hooks", {}) or {}

    for event, gen_entries in generated_hooks.items():
        existing_hooks[event] = reconcile_bucket(
            existing_hooks.get(event, []) or [], gen_entries
        )

    result["hooks"] = existing_hooks
    result["version"] = 1
    return result


def reconcile_claude(existing: dict, generated: dict) -> dict:
    """Reconcile a Claude settings.json document's ``hooks`` key.

    The generated payload is the bare ``{EVENT: [group, ...]}`` fragment
    (Claude uses capitalized event names: PreToolUse/PostToolUse/...).
    """
    result = dict(existing)
    existing_hooks: dict = dict(existing.get("hooks", {}) or {})

    for event, gen_groups in generated.items():
        existing_hooks[event] = reconcile_bucket(
            existing_hooks.get(event, []) or [], gen_groups
        )

    result["hooks"] = existing_hooks
    return result


RECONCILERS = {
    "cursor": reconcile_cursor,
    "claude": reconcile_claude,
}


def _load_existing(path: str) -> dict:
    """Load the existing target file, tolerating missing/empty/invalid files."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except FileNotFoundError:
        return {}
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def main() -> None:
    if len(sys.argv) < 3:
        print(
            "Usage: hooks_reconciler.py <cursor|claude> <existing-file>",
            file=sys.stderr,
        )
        sys.exit(1)

    kind = sys.argv[1]
    existing_path = sys.argv[2]

    if kind not in RECONCILERS:
        print(f"Unknown kind: {kind}. Use: {', '.join(RECONCILERS)}", file=sys.stderr)
        sys.exit(1)

    generated = json.loads(sys.stdin.read())
    existing = _load_existing(existing_path)
    result = RECONCILERS[kind](existing, generated)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
