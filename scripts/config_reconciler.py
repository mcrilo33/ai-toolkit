#!/usr/bin/env python3
"""Reconcile ai-toolkit-managed config into a target host config file.

Companion to ``hooks_reconciler.py`` (issue #333). The sync pipeline ships a set
of shared config files (``pyproject.toml``, ``ruff.toml``, ``.gitignore``,
``.editorconfig``, ``.python-version``). Copying them once and skipping forever
means a host never gains toolkit-introduced fields nor sheds deprecated ones;
clobbering them destroys host-authored content. The safe middle ground is an
ownership-aware reconcile — but only formats whose syntax a stdlib-only,
comment/ordering-preserving pass can round-trip safely qualify.

Tier assignment (the design gate — issue #333):

    Format             Tier  Rationale
    ────────────────── ────  ──────────────────────────────────────────────────
    .gitignore         (b)   Line-based, no parser needed. A sentinel-marked
                             managed block is replaced wholesale while every host
                             line outside it is preserved byte-for-byte. Mirrors
                             hooks_reconciler's owned-set model; converges to a
                             fixed point. Reconciled here.
    pyproject.toml     (a)   TOML with comments/ordering and heavy host-authored
                             content; comment-preserving rewrite needs tomlkit
                             (third-party). tomllib is read-only. Protection-only.
    ruff.toml          (a)   Same TOML constraint. Protection-only.
    .editorconfig      (a)   INI-with-glob-sections + comments; configparser will
                             not round-trip comments/ordering. Protection-only.
    .python-version    (a)   A single host-owned value; there is no host-authored
                             region to preserve alongside a managed one — a
                             reconcile would merely clobber the host's choice.
                             Protection-only.

Tier (a) formats stay copy-if-absent in sync-to-repo.sh and are explicitly
GC-protected in sync_manifest.py; extending them to tier (b) is a per-format
follow-up (each needs its own safe writer). This module handles only tier (b).

The ``.gitignore`` ownership model mirrors ``hooks_reconciler.py``:

  1. Remove every well-formed managed block (BEGIN..END inclusive) from the
     existing file — this both replaces the old owned set and self-heals a file
     that somehow accrued duplicate blocks.
  2. Trim trailing blank lines from the surviving host remainder so repeated
     runs do not accumulate whitespace.
  3. Append exactly one fresh managed block containing the current managed set.

Host lines outside the markers are never touched; the result is a fixed point on
repeated syncs (idempotent). A malformed block (a BEGIN with no matching END) is
left in place as host content and a fresh block is appended — a loud, harmless
degradation rather than silent data loss.

Usage:
    python3 config_reconciler.py gitignore <existing-file-or-empty> < managed

Reads the managed content (shared/.gitignore) on stdin, reads the existing
target file (path may be empty/non-existent), writes the reconciled full file to
stdout. Can also be imported as a module for unit testing. Python 3 stdlib only.
"""

from __future__ import annotations

import sys

# Sentinel markers bounding the ai-toolkit-owned region. Invariant: the managed
# content (shared/.gitignore) must never itself contain a marker line, or the
# wrapped block would be mis-parsed on the next reconcile.
BEGIN_MARKER = "# >>> ai-toolkit managed (do not edit) >>>"
END_MARKER = "# <<< ai-toolkit managed <<<"


def _strip_managed_blocks(lines: list[str]) -> list[str]:
    """Return ``lines`` with every well-formed managed block removed.

    A block is a line equal to ``BEGIN_MARKER`` (ignoring surrounding
    whitespace) followed by an ``END_MARKER`` line with no intervening BEGIN;
    both markers and every line between them are dropped. A BEGIN that hits EOF
    or another BEGIN before an END is a true orphan and is kept in place
    (treated as host content) — otherwise it would greedily swallow the next
    block's END and break idempotence.
    """
    result: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() == BEGIN_MARKER:
            j = i + 1
            while j < n and lines[j].strip() not in (BEGIN_MARKER, END_MARKER):
                j += 1
            if j < n and lines[j].strip() == END_MARKER:
                i = j + 1  # well-formed block — skip it whole
                continue
        result.append(lines[i])
        i += 1
    return result


def _rstrip_blank(lines: list[str]) -> list[str]:
    """Drop trailing empty/whitespace-only lines."""
    end = len(lines)
    while end > 0 and lines[end - 1].strip() == "":
        end -= 1
    return lines[:end]


def reconcile_gitignore(existing: str, managed: str) -> str:
    """Reconcile the ai-toolkit managed block into a host ``.gitignore``.

    Args:
        existing: Current target file content (``""`` if absent).
        managed: The ai-toolkit-owned pattern set (shared/.gitignore content).

    Returns:
        The reconciled file: host lines preserved byte-for-byte outside the
        markers, followed by exactly one managed block carrying ``managed``.
        Idempotent — reconciling the result again yields the same string.
    """
    host = _rstrip_blank(_strip_managed_blocks(existing.split("\n")))

    body = managed.strip("\n")
    block = [BEGIN_MARKER, *(body.split("\n") if body else []), END_MARKER]

    parts: list[str] = []
    if host:
        parts.extend(host)
        parts.append("")  # blank line separating host content from the block
    parts.extend(block)
    return "\n".join(parts) + "\n"


RECONCILERS = {
    "gitignore": reconcile_gitignore,
}


def _load_existing(path: str) -> str:
    """Load the existing target file, tolerating a missing/empty path."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def main() -> None:
    if len(sys.argv) < 3:
        print(
            "Usage: config_reconciler.py <gitignore> <existing-file>",
            file=sys.stderr,
        )
        sys.exit(1)

    kind = sys.argv[1]
    existing_path = sys.argv[2]

    if kind not in RECONCILERS:
        print(f"Unknown kind: {kind}. Use: {', '.join(RECONCILERS)}", file=sys.stderr)
        sys.exit(1)

    managed = sys.stdin.read()
    existing = _load_existing(existing_path)
    sys.stdout.write(RECONCILERS[kind](existing, managed))


if __name__ == "__main__":
    main()
