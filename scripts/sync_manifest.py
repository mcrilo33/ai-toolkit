#!/usr/bin/env python3
"""Sync manifest bookkeeping and stale-file garbage collection.

Maintains ``.ai-toolkit-manifest.json`` at a sync target's root: records the
files written by each tool's sync and deletes files that a previous sync wrote
but the current one no longer produces (stale outputs). Files not listed in
the manifest are never touched.

Called by sync-to-repo.sh via
``python3 scripts/sync_manifest.py finalize <target> <tool> <rev> [--dry-run]``
with repo-relative paths on stdin (newline-separated). Can also be imported as
a module for unit testing. Python 3 stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath

MANIFEST_NAME = ".ai-toolkit-manifest.json"

# Hook-reconciler-owned configs and backups are never deleted by GC.
_PROTECTED_PATHS = frozenset({".cursor/hooks.json", ".claude/settings.json"})

# Host-owned config files (issue #333). These are copy-if-absent or
# reconciled-in-place, never regenerated wholesale, so an already-existing one
# is host-owned and must never be GC-removed — made explicit here rather than
# relying on the paths merely being absent from the manifest.
_PROTECTED_CONFIG_PATHS = frozenset(
    {
        "pyproject.toml",
        "ruff.toml",
        ".gitignore",
        ".editorconfig",
        ".python-version",
    }
)


def _validate_relpath(path: str) -> None:
    """Raise ValueError if ``path`` is absolute or escapes the target root."""
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise ValueError(f"absolute path not allowed: {path}")
    if ".." in pure.parts:
        raise ValueError(f"path traversal not allowed: {path}")


def _is_protected(path: str) -> bool:
    return path in _PROTECTED_PATHS or path in _PROTECTED_CONFIG_PATHS or path.endswith(".bak")


def _is_contained(target_dir: Path, path: str) -> bool:
    """True when ``target_dir/path`` resolves inside ``target_dir``.

    Guards against symlink escapes: a validated relative path under a
    symlinked parent (e.g. ``link/evil.txt`` where ``link`` points outside
    the target) must never be deleted.
    """
    full = (target_dir / path).resolve()
    return full.is_relative_to(target_dir.resolve())


def _load_manifest(target: Path) -> dict:
    """Return the parsed manifest, or an empty one if missing or corrupt."""
    manifest_file = target / MANIFEST_NAME
    if not manifest_file.exists():
        return {"tools": {}}
    try:
        data = json.loads(manifest_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"tools": {}}
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        return {"tools": {}}
    return data


def _write_manifest(target: Path, manifest: dict) -> None:
    """Write the manifest with stable, byte-reproducible serialization."""
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (target / MANIFEST_NAME).write_text(text)


def finalize(
    target: str,
    tool: str,
    files: list[str],
    *,
    toolkit_rev: str,
    dry_run: bool = False,
) -> list[str]:
    """Update the sync manifest for ``tool`` and GC stale toolkit outputs.

    Paths listed under ``tool`` in the old manifest but absent from ``files``
    are deleted from disk (stale outputs of a previous sync). Paths never
    listed in the manifest are never touched. Protected paths (reconciler-owned
    configs, host-owned config files, ``*.bak`` backups) are never deleted.

    Args:
        target: Path to the target repo root.
        tool: Tool whose file list is being finalized (e.g. ``cursor``).
        files: Repo-relative paths written by this sync run.
        toolkit_rev: Toolkit git revision to record in the manifest.
        dry_run: If True, report would-be deletions without touching disk
            or the manifest.

    Returns:
        Sorted repo-relative paths that were deleted (or would be).

    Raises:
        ValueError: If any input or old-manifest path is absolute or contains
            a ``..`` traversal segment.
    """
    target_dir = Path(target)
    new_files = sorted(set(files))
    for path in new_files:
        _validate_relpath(path)

    manifest = _load_manifest(target_dir)
    old_files = [p for p in manifest["tools"].get(tool, []) if isinstance(p, str)]
    for path in old_files:
        _validate_relpath(path)

    stale = sorted(set(old_files) - set(new_files))
    deleted = [p for p in stale if not _is_protected(p) and _is_contained(target_dir, p)]
    if dry_run:
        return deleted

    for path in deleted:
        (target_dir / path).unlink(missing_ok=True)
    tools = dict(manifest["tools"])
    tools[tool] = new_files
    _write_manifest(target_dir, {"toolkit_rev": toolkit_rev, "tools": tools})
    return deleted


def main() -> None:
    """CLI entry point — called by sync-to-repo.sh."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fin = sub.add_parser("finalize", help="Update manifest and GC stale files")
    fin.add_argument("target", help="Target repo root")
    fin.add_argument("tool", help="Tool name (copilot, cursor, claude)")
    fin.add_argument("rev", help="Toolkit git revision")
    fin.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    files = [line.strip() for line in sys.stdin if line.strip()]
    deleted = finalize(args.target, args.tool, files, toolkit_rev=args.rev, dry_run=args.dry_run)
    for path in deleted:
        print(path)


if __name__ == "__main__":
    main()
