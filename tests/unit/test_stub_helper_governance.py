"""Governance: every executable test stub is written through `tests/_stubs.write_stub` (#376).

A freshly written script's FIRST exec on macOS can take seconds under xdist (the OS vets each
new executable) while a re-exec takes ~10ms. A test that writes a raw `chmod +x` stub and then
races it against a bounded wait is therefore a latent flake (#374/#375). `write_stub` warms the
stub up front; this test fails when a test file marks a file executable any other way.

Known blind spots (AST scan of `<x>.chmod(<literal or S_IX*>)` only): mode-preserving copies of
executable scripts (`shutil.copy*`), a mode held in a variable, a bare `chmod(...)` import, and
`chmod +x` inside a stub's shell body.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]

# Names whose appearance in a `chmod` call's mode argument sets an executable bit.
_EXEC_BIT_NAMES = {"S_IEXEC", "S_IXUSR", "S_IXGRP", "S_IXOTH"}

# "<path relative to tests/>" (whole file) or "<path>::<function>" -> why a raw exec bit is
# correct there.
ALLOWLIST: dict[str, str] = {
    "unit/test_archive_worktree.py::test_unwritable_spool_root_exits_zero_with_stderr_only": (
        "chmods a directory read-only, not a stub"
    ),
    "unit/test_commit_hooks.py::test_docs_executable_md_still_requires_anchor": (
        "the fixture IS an executable markdown file, not a stub"
    ),
    "unit/_orca_stub.py::install_orca_stub": (
        "a python-shebang stub; write_stub's warm guard is shell syntax (follow-up: python-aware warm)"
    ),
    "unit/test_install_git_hooks.py::_foreign": (
        "a foreign hook the installer must preserve byte-for-byte; the warm guard would alter it"
    ),
    "unit/test_worktree_foreign_checkout.py::synced_repo": (
        "copies the repo's real scripts into a fixture checkout, not synthetic stubs"
    ),
    "unit/test_stubs.py::test_warm_stubs_raises_when_a_stub_cannot_run": (
        "builds a deliberately unrunnable script to exercise the helper's failure path"
    ),
}


def _mode_sets_exec_bit(node: ast.expr) -> bool:
    """True when a chmod mode expression can set an executable bit."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return bool(node.value & 0o111)
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in _EXEC_BIT_NAMES:
            return True
        if isinstance(sub, ast.Name) and sub.id in _EXEC_BIT_NAMES:
            return True
    return False


def find_raw_exec_files(source: str) -> list[str]:
    """Names of the functions in `source` that chmod a file executable (`<module>` at top level)."""
    found: list[str] = []

    def visit(node: ast.AST, scope: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "chmod"
            and any(_mode_sets_exec_bit(a) for a in node.args)
        ):
            found.append(scope)
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(ast.parse(source), "<module>")
    return found


def _violations() -> list[str]:
    out: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        rel = path.relative_to(TESTS_DIR).as_posix()
        if rel == "_stubs.py" or rel in ALLOWLIST:
            continue
        for scope in find_raw_exec_files(path.read_text()):
            if f"{rel}::{scope}" not in ALLOWLIST:
                out.append(f"{rel}::{scope}")
    return sorted(set(out))


def test_no_test_file_chmods_a_stub_executable_outside_the_helper() -> None:
    assert _violations() == [], (
        "these tests mark a file executable without tests/_stubs.write_stub "
        "(cold first exec flakes under xdist, #376); use write_stub or add a justified ALLOWLIST entry"
    )


def test_scanner_flags_a_new_raw_chmod_stub() -> None:
    src = (
        "def test_x(tmp_path):\n"
        "    stub = tmp_path / 'tmux'\n"
        "    stub.write_text('#!/usr/bin/env bash\\nexit 0\\n')\n"
        "    stub.chmod(0o755)\n"
    )

    assert find_raw_exec_files(src) == ["test_x"]


def test_scanner_flags_os_chmod_and_stat_bit_forms() -> None:
    src = (
        "import os, stat\n"
        "def test_a(p):\n"
        "    os.chmod(p, 0o700)\n"
        "def test_b(p):\n"
        "    p.chmod(p.stat().st_mode | stat.S_IEXEC)\n"
    )

    assert find_raw_exec_files(src) == ["test_a", "test_b"]


def test_scanner_ignores_non_executable_modes() -> None:
    src = "def test_x(p):\n    p.chmod(0o000)\n    p.chmod(0o644)\n"

    assert find_raw_exec_files(src) == []


def test_allowlist_entries_still_match_a_raw_exec_chmod() -> None:
    stale = []
    for entry in ALLOWLIST:
        rel, _, scope = entry.partition("::")
        path = TESTS_DIR / rel
        scopes = find_raw_exec_files(path.read_text()) if path.is_file() else []
        if not scopes or (scope and scope not in scopes):
            stale.append(entry)

    assert stale == [], f"stale ALLOWLIST entries (remove them): {stale}"
