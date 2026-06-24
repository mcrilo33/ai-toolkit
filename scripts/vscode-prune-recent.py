#!/usr/bin/env python3
"""Remove one folder path from VS Code's "Open Recent" list (issue #103).

`worktree-done.sh` deletes a task worktree on teardown, but VS Code keeps the
path in its global recent-folders history forever — one stale `ai-toolkit-N`
entry per spoke ever run. `code --remove` only folds the folder out of the live
multi-root window; it does not touch the recent list, and the CLI exposes no
supported way to drop a single recent entry. So this scrubs the entry directly
from the global state store.

The recent folders live in `storage.json` under `lastKnownMenubarData` (the
File → Open Recent submenu); older versions also keep them in the top-level
`history.recentlyOpenedPathsList` key. This removes the matching path from both.

Usage:
    vscode-prune-recent.py <storage.json> <worktree-path>

Best-effort by contract: it writes only when something changed, writes
atomically (temp file + rename) so a concurrent reader never sees a partial
file, and exits 0 on any structural surprise rather than failing teardown. The
caller is responsible for skipping this when VS Code is running — a live
instance overwrites storage.json on flush.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any
from urllib.parse import unquote


def _folder_uri_path(uri: str) -> str | None:
    """Return the filesystem path of a `file://` recent-list URI, else None.

    VS Code percent-encodes URIs (e.g. a space becomes `%20`), so decode before
    comparing against a raw filesystem path — the submenu stores the decoded path
    directly, so both match strategies must agree.
    """
    if isinstance(uri, str) and uri.startswith("file://"):
        return unquote(uri[len("file://") :])
    return None


def _scrub_menubar(data: dict[str, Any], target: str) -> bool:
    """Drop recent submenu entries whose path equals `target`. Returns changed."""
    menus = data.get("lastKnownMenubarData", {}).get("menus", {})
    file_menu = menus.get("File", {}) if isinstance(menus, dict) else {}
    changed = False
    for item in file_menu.get("items", []) or []:
        submenu = item.get("submenu") if isinstance(item, dict) else None
        if not isinstance(submenu, dict):
            continue
        entries = submenu.get("items")
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if _menu_entry_path(e) != target]
        if len(kept) != len(entries):
            submenu["items"] = kept
            changed = True
    return changed


def _menu_entry_path(entry: Any) -> str | None:
    if isinstance(entry, dict):
        uri = entry.get("uri")
        if isinstance(uri, dict):
            path = uri.get("path")
            if isinstance(path, str):
                return path
    return None


def _scrub_history(data: dict[str, Any], target: str) -> bool:
    """Drop `history.recentlyOpenedPathsList` entries for `target`. Returns changed."""
    history = data.get("history.recentlyOpenedPathsList")
    if not isinstance(history, dict):
        return False
    entries = history.get("entries")
    if not isinstance(entries, list):
        return False
    kept = [e for e in entries if not _history_entry_matches(e, target)]
    if len(kept) == len(entries):
        return False
    history["entries"] = kept
    return True


def _history_entry_matches(entry: Any, target: str) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(_folder_uri_path(entry.get(key, "")) == target for key in ("folderUri", "fileUri"))


def _atomic_write(storage: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(storage) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".storage-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        # mkstemp creates the temp file 0600; carry over the store's real mode
        # (typically 0644) so the rewrite doesn't silently tighten permissions.
        os.chmod(tmp, os.stat(storage).st_mode & 0o777)
        os.replace(tmp, storage)
    except BaseException:  # incl. KeyboardInterrupt — drop the temp file, then reraise
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <storage.json> <worktree-path>", file=sys.stderr)
        return 2
    storage, target = argv[1], argv[2]
    try:
        with open(storage, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return 0
        changed = _scrub_menubar(data, target)
        changed = _scrub_history(data, target) or changed
        if changed:
            _atomic_write(storage, data)
            print(f"  pruned {target} from VS Code's Open Recent list.")
    except (OSError, ValueError) as e:
        # Best-effort: a missing/locked/malformed store must never fail teardown.
        print(f"  skipped VS Code Open Recent cleanup ({e}).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
