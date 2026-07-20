"""Governance (issue #326): every shipped ``.sh`` reaches a collectible
``test_*.py`` — either directly (a test names its basename as a token) or through
the shell source-dependency graph (a tested script transitively ``source``s it).

testmon is blind to shell, so a ``.sh`` that reaches no test is invisible to the
pre-push gate's SELECTED tier and silently forces the full suite forever. This
guard turns that into a RED at commit time: a new script reachable to no test
fails here instead. The reachability it checks is the SAME map the selector uses
(``reverse_index_tests_for`` in ``lib/test-reverse-index.sh``), so the guard and
the gate can never drift.

The real-repo case (``test_every_shipped_sh_reaches_a_test``) is the standing
enforcement loop. The hermetic cases pin the graph-aware behaviour: a lib reachable
ONLY through the source graph counts as covered (the assertion that fails before
#326), and a genuinely orphaned script is still flagged.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "shared" / "hooks" / "lib" / "test-reverse-index.sh"

# Pin git config to nothing so a host's global config can't reach these repos.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

# The shipped shell surface: the SAME dirs the selector's reverse index scans
# (REVERSE_INDEX_SHELL_DIRS in lib/test-reverse-index.sh), kept in lock-step so
# the guard can never under-cover relative to the gate — a future orphan .sh under
# dashboard/langfuse or a skills root must still be caught, not silently skipped.
SHELL_DIRS = ("scripts", "shared/hooks", "shared/skills", "dashboard/langfuse")


def _shipped_sh(root: Path) -> list[Path]:
    return sorted(
        f
        for d in (root / p for p in SHELL_DIRS)
        if d.is_dir()
        for f in d.rglob("*.sh")
        if f.is_file()
    )


def _lookup(root: Path, rel: str) -> list[str]:
    """Test files the reverse index maps `rel` to (direct plus source graph)."""
    proc = subprocess.run(
        ["bash", "-c", f'source "{LIB}" && reverse_index_tests_for "$1"', "_", rel],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


def _exempt_entries(root: Path) -> list[str]:
    exempt = root / ".test-select-exempt"
    if not exempt.is_file():
        return []
    entries = []
    for raw in exempt.read_text().splitlines():
        entry = raw.split("#", 1)[0].strip().rstrip("/")
        if entry:
            entries.append(entry)
    return entries


def _is_exempt(rel: str, entries: list[str]) -> bool:
    return any(rel == e or rel.startswith(e + "/") for e in entries)


def _unreachable_shell_scripts(root: Path) -> list[str]:
    """Shipped .sh that map to no test and are not exempt."""
    entries = _exempt_entries(root)
    out = []
    for f in _shipped_sh(root):
        rel = str(f.relative_to(root))
        if _is_exempt(rel, entries):
            continue
        if not _lookup(root, rel):
            out.append(rel)
    return out


def test_every_shipped_sh_reaches_a_test() -> None:
    unreachable = _unreachable_shell_scripts(REPO_ROOT)
    assert unreachable == [], (
        "shipped .sh reachable to no test (directly or via the source graph): "
        + ", ".join(unreachable)
        + " — add a tests/**/test_*.py naming its basename, source it from a tested "
        "script, or (only for a file with legitimately no test surface) add a "
        ".test-select-exempt entry"
    )


# --- hermetic: the graph-aware reachability behaviour ---------------------------


def _init_repo(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        subprocess.run(
            ["git", "config", k, v], cwd=str(root), check=True, capture_output=True, env=_GIT_ENV
        )


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _commit(root: Path) -> None:
    subprocess.run(
        ["git", "add", "-A"], cwd=str(root), check=True, capture_output=True, env=_GIT_ENV
    )
    subprocess.run(
        ["git", "commit", "-qm", "seed"],
        cwd=str(root),
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )


def test_graph_only_reachable_lib_counts_as_covered(tmp_path: Path) -> None:
    # base.sh has no test of its own; it is sourced by a tested consumer, so the
    # source graph makes it reachable. Before #326 the graph does not exist and
    # base.sh looks unreachable — this is the governance assertion's RED proof.
    root = tmp_path / "repo"
    _init_repo(root)
    _write(root, "README.md", "seed\n")
    _write(root, "tests/unit/test_consumer.py", 'HOOK = "consumer.sh"\n')
    _write(root, "shared/hooks/consumer.sh", '#!/bin/sh\nsource "$D/lib/base.sh"\n')
    _write(root, "shared/hooks/lib/base.sh", "#!/bin/sh\n")
    _commit(root)

    assert _unreachable_shell_scripts(root) == []


def test_orphan_script_is_flagged(tmp_path: Path) -> None:
    # A script with no test and no tested dependent IS reported — the guard
    # actually fails when it should.
    root = tmp_path / "repo"
    _init_repo(root)
    _write(root, "README.md", "seed\n")
    _write(root, "scripts/orphan.sh", "#!/bin/sh\n")
    _commit(root)

    assert _unreachable_shell_scripts(root) == ["scripts/orphan.sh"]
