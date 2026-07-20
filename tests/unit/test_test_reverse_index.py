"""Unit tests for shared/hooks/lib/test-reverse-index.sh — the changed-file →
referencing-test-files map behind issue #123's SELECTED tier.

The convention this repo already follows is that a test names the script it
covers as a literal token — either the full repo path
(``shared/skills/hub/scripts/hub-afk.sh`` in a docstring) or the basename in a
path build (``HOOKS_DIR / "commit-gauntlet.sh"``). The lib scans ``tests/`` for
filename-shaped tokens and maps a changed file to the ``test_*.py`` files that
mention its basename as an EXACT token — ``chmod-scope-guard.sh`` must never
count as a reference to ``scope-guard.sh`` (the substring trap found while
surveying #123).

The map is cached under ``<git-common-dir>/.test-reverse-index/<key>`` keyed on
the tree hash of ``tests/`` at HEAD (structural invalidation, exactly like the
gate stamps of #122). A dirty ``tests/`` bypasses the cache with a fresh scan of
the working tree so a just-written test is visible to the commit-time nudge
before it is ever committed.

Hermetic, like test_test_select.py: a throwaway git repo per test; the lib is
sourced by a bash child whose cwd is the repo root (the contract test-select.sh
and commit-gauntlet.sh use).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "shared" / "hooks" / "lib" / "test-reverse-index.sh"

# Pin git config to nothing so a host's global config can't reach these repos.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=_GIT_ENV
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repo on `main` with one seed commit and an empty tests/ layout."""
    r = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(r)], check=True, capture_output=True, env=_GIT_ENV
    )
    for k, v in (("user.email", "t@t.t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(r, "config", k, v)
    (r / "README.md").write_text("seed\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "chore: seed")
    return r


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _commit_all(repo: Path, msg: str = "change") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


def _tests_for(repo: Path, changed_file: str) -> subprocess.CompletedProcess[str]:
    """Source the lib in a bash child (cwd = repo root) and look up one file."""
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}" && reverse_index_tests_for "$1"', "_", changed_file],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def _lookup(repo: Path, changed_file: str) -> list[str]:
    proc = _tests_for(repo, changed_file)
    assert proc.returncode == 0, proc.stderr
    return [line for line in proc.stdout.splitlines() if line]


def _cache_path(repo: Path) -> Path:
    """Where the lib must cache the map for the repo's committed tests/ tree."""
    key = _git(repo, "rev-parse", "HEAD:tests").strip()
    return repo / ".git" / ".test-reverse-index" / key


# --- token matching: the reference conventions the repo already uses -------------


def test_basename_in_path_build_maps_to_test_file(repo: Path) -> None:
    _write(
        repo,
        "tests/unit/test_guard.py",
        'HOOK = ROOT / "shared" / "hooks" / "guard.sh"\n',
    )
    _write(repo, "shared/hooks/guard.sh", "#!/bin/sh\n")
    _commit_all(repo)

    assert _lookup(repo, "shared/hooks/guard.sh") == ["tests/unit/test_guard.py"]


def test_full_path_reference_maps_via_basename_token(repo: Path) -> None:
    _write(
        repo,
        "tests/unit/test_other.py",
        '"""Covers shared/hooks/other-hook.sh end to end."""\n',
    )
    _write(repo, "shared/hooks/other-hook.sh", "#!/bin/sh\n")
    _commit_all(repo)

    assert _lookup(repo, "shared/hooks/other-hook.sh") == ["tests/unit/test_other.py"]


def test_unreferenced_script_maps_to_nothing(repo: Path) -> None:
    _write(repo, "tests/unit/test_guard.py", 'HOOK = "guard.sh"\n')
    _write(repo, "scripts/orphan.sh", "#!/bin/sh\n")
    _commit_all(repo)

    assert _lookup(repo, "scripts/orphan.sh") == []


def test_basename_substring_is_not_a_reference(repo: Path) -> None:
    # The survey trap: chmod-scope-guard.sh contains "scope-guard.sh" as a
    # substring, but only exact-token matches count.
    _write(
        repo,
        "tests/unit/test_chmod.py",
        'HOOK = HOOKS / "chmod-scope-guard.sh"\n',
    )
    _write(repo, "shared/hooks/lib/scope-guard.sh", "#!/bin/sh\n")
    _commit_all(repo)

    assert _lookup(repo, "shared/hooks/lib/scope-guard.sh") == []


def test_two_referencing_tests_both_returned_sorted(repo: Path) -> None:
    _write(repo, "tests/unit/test_b.py", '"""scripts/deploy.sh — tier checks."""\n')
    _write(repo, "tests/unit/test_a.py", 'SCRIPT = SCRIPTS / "deploy.sh"\n')
    _write(repo, "scripts/deploy.sh", "#!/bin/sh\n")
    _commit_all(repo)

    assert _lookup(repo, "scripts/deploy.sh") == [
        "tests/unit/test_a.py",
        "tests/unit/test_b.py",
    ]


def test_yaml_and_extensionless_wellknown_names_map(repo: Path) -> None:
    _write(
        repo,
        "tests/unit/test_stack.py",
        '"""Validates dashboard/langfuse/otelcol.yaml and the Dockerfile."""\n',
    )
    _write(repo, "dashboard/langfuse/otelcol.yaml", "receivers: {}\n")
    _write(repo, "Dockerfile", "FROM scratch\n")
    _commit_all(repo)

    assert _lookup(repo, "dashboard/langfuse/otelcol.yaml") == ["tests/unit/test_stack.py"]
    assert _lookup(repo, "Dockerfile") == ["tests/unit/test_stack.py"]


def test_reference_in_non_test_file_does_not_count(repo: Path) -> None:
    # Only collectible test_*.py files are valid map targets: a fixture or
    # conftest mentioning a script is not a runnable selection.
    _write(repo, "tests/fixtures/data.py", 'PATH = "scripts/tool.sh"\n')
    _write(repo, "tests/conftest.py", '"""scripts/tool.sh shim."""\n')
    _write(repo, "scripts/tool.sh", "#!/bin/sh\n")
    _commit_all(repo)

    assert _lookup(repo, "scripts/tool.sh") == []


def test_lookup_survives_tokenless_test_file_under_pipefail(repo: Path) -> None:
    # A test file containing no filename-shaped token makes the scanner's grep
    # exit 1; under test-select.sh's `set -euo pipefail` that must neither abort
    # the lookup nor truncate the map at that file (subtask A review finding).
    _write(repo, "tests/unit/test_aaa.py", 'S = SCRIPTS / "shared.sh"\n')
    _write(repo, "tests/unit/test_mmm.py", "def test_it():\n    assert 1 + 1 == 2\n")
    _write(repo, "tests/unit/test_zzz.py", '"""Covers scripts/shared.sh too."""\n')
    _write(repo, "scripts/shared.sh", "#!/bin/sh\n")
    _commit_all(repo)
    # Dirty tests/ forces the inline fresh-scan path — the exposed one.
    _write(repo, "tests/unit/test_new.py", "def test_more():\n    pass\n")

    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; source "{LIB}" && reverse_index_tests_for "$1"',
            "_",
            "scripts/shared.sh",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )

    assert proc.returncode == 0, proc.stderr
    assert [line for line in proc.stdout.splitlines() if line] == [
        "tests/unit/test_aaa.py",
        "tests/unit/test_zzz.py",
    ]


# --- the cache: keyed on the tree hash of tests/, shared via git-common-dir ------


def test_lookup_mints_cache_keyed_on_tests_tree_hash(repo: Path) -> None:
    _write(repo, "tests/unit/test_guard.py", 'HOOK = "guard.sh"\n')
    _commit_all(repo)

    _lookup(repo, "shared/hooks/guard.sh")

    assert _cache_path(repo).is_file()


def test_clean_tree_lookup_consumes_cache_without_rescan(repo: Path) -> None:
    _write(repo, "tests/unit/test_guard.py", 'HOOK = "guard.sh"\n')
    _commit_all(repo)
    _lookup(repo, "shared/hooks/guard.sh")  # first call mints the cache
    # Plant a synthetic mapping: if the second lookup rescanned instead of
    # consuming the cache, the planted entry could not be returned.
    _cache_path(repo).write_text("planted.sh\ttests/unit/test_planted.py\n")

    assert _lookup(repo, "scripts/planted.sh") == ["tests/unit/test_planted.py"]


def test_dirty_tests_dir_bypasses_cache_with_fresh_scan(repo: Path) -> None:
    _write(repo, "tests/unit/test_guard.py", 'HOOK = "guard.sh"\n')
    _commit_all(repo)
    _lookup(repo, "shared/hooks/guard.sh")  # mints the cache for HEAD:tests
    planted = "planted.sh\ttests/unit/test_planted.py\n"
    _cache_path(repo).write_text(planted)
    # An UNCOMMITTED new test file: the nudge must see it before any commit.
    _write(repo, "tests/unit/test_new.py", 'SCRIPT = "newscript.sh"\n')

    assert _lookup(repo, "scripts/newscript.sh") == ["tests/unit/test_new.py"]
    assert _lookup(repo, "scripts/planted.sh") == []  # cache not consulted …
    assert _cache_path(repo).read_text() == planted  # … and not overwritten


def test_committing_tests_change_mints_new_cache_key(repo: Path) -> None:
    _write(repo, "tests/unit/test_guard.py", 'HOOK = "guard.sh"\n')
    _commit_all(repo)
    _lookup(repo, "shared/hooks/guard.sh")
    old_cache = _cache_path(repo)

    _write(repo, "tests/unit/test_new.py", 'SCRIPT = "newscript.sh"\n')
    _commit_all(repo, "test: add newscript coverage")

    assert _lookup(repo, "scripts/newscript.sh") == ["tests/unit/test_new.py"]
    new_cache = _cache_path(repo)
    assert new_cache != old_cache  # structural invalidation: new tests/ tree, new key
    assert new_cache.is_file()


def test_repo_without_tests_dir_degrades_to_empty(repo: Path) -> None:
    _write(repo, "scripts/tool.sh", "#!/bin/sh\n")
    _commit_all(repo)

    assert _lookup(repo, "scripts/tool.sh") == []


# --- the enforcement meta-test: every control-plane script is referenced (#123) ---
# These run against the REAL repo tree — they ARE the enforcement loop: an
# unmapped control-plane script escalates the push to the full suite, the full
# suite contains this class, and this class stays red until a referencing test
# exists. Unmapped can exist locally but can never land.
#
# Ceiling, stated honestly: "referenced" means the basename appears as a token
# in some test_*.py — including a fixture-data mention that never executes the
# script. The gate enforces reference, not exercise; review still owns quality.

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_DIRS = ("scripts", "shared/hooks", "dashboard/langfuse")
CONTROL_PLANE_GLOB_DIRS = ("shared/skills/*/scripts",)
CONTROL_PLANE_SUFFIXES = (".sh", ".yml", ".yaml")
EXEMPT_LIST = REPO_ROOT / ".test-select-exempt"


def _control_plane_files() -> list[Path]:
    dirs = [REPO_ROOT / d for d in CONTROL_PLANE_DIRS]
    for pattern in CONTROL_PLANE_GLOB_DIRS:
        dirs.extend(REPO_ROOT.glob(pattern))
    return sorted(
        f
        for d in dirs
        if d.is_dir()
        for f in d.rglob("*")
        if f.is_file() and f.suffix in CONTROL_PLANE_SUFFIXES
    )


def _exempt_entries() -> list[str]:
    if not EXEMPT_LIST.is_file():
        return []
    entries = []
    for raw in EXEMPT_LIST.read_text().splitlines():
        entry = raw.split("#", 1)[0].strip().rstrip("/")
        if entry:
            entries.append(entry)
    return entries


def _is_exempt(rel: str, entries: list[str]) -> bool:
    return any(rel == e or rel.startswith(e + "/") for e in entries)


def _scan_real_map_tokens() -> set[str]:
    """Dump the real tree's map via the SAME lib the selector uses."""
    proc = subprocess.run(
        ["bash", "-c", f'source "{LIB}" && _reverse_index_scan'],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=True,
    )
    return {line.split("\t", 1)[0] for line in proc.stdout.splitlines() if "\t" in line}


class TestControlPlaneCoverage:
    def test_every_control_plane_script_is_referenced_or_exempt(self) -> None:
        tokens = _scan_real_map_tokens()
        entries = _exempt_entries()
        unmapped = [
            str(f.relative_to(REPO_ROOT))
            for f in _control_plane_files()
            if f.name not in tokens and not _is_exempt(str(f.relative_to(REPO_ROOT)), entries)
        ]
        assert unmapped == [], (
            "control-plane files with no referencing test: "
            + ", ".join(unmapped)
            + " — add a tests/**/test_*.py referencing each (or, only for a file "
            "with legitimately no test surface, a .test-select-exempt entry)"
        )

    def test_exempt_entries_exist_on_disk(self) -> None:
        stale = [e for e in _exempt_entries() if not (REPO_ROOT / e).exists()]
        assert stale == [], f".test-select-exempt names missing paths: {stale}"


# --- straggler smoke tests: the scripts the #123 survey found unreferenced -------
# Their literal paths here are themselves the references that satisfy the
# coverage meta-test — the smoke is minimal but real (the script must parse).


@pytest.mark.parametrize(
    "script",
    [
        "scripts/install.sh",
        "scripts/list-cursor-rules.sh",
        "shared/hooks/lib/scope-guard.sh",
    ],
)
def test_straggler_script_parses(script: str) -> None:
    proc = subprocess.run(["bash", "-n", str(REPO_ROOT / script)], capture_output=True, text=True)
    assert proc.returncode == 0, f"{script} does not parse: {proc.stderr}"


# --- issue #326: the shell source-dependency graph -------------------------------
# A changed sourced library maps not only to tests naming it verbatim but to the
# mirror tests of every script that (transitively) `source`s it. testmon is blind
# to shell, so this is the only thing that can prune a lib change to a SELECTED run
# instead of the full suite. Over-map, never under-map: an unresolvable `$var`
# source drops an edge (backstopped by the #124 post-land sweep), never a wrong
# mapping.


def _cache_keys(repo: Path) -> list[str]:
    """The cache-key filenames present under <git-dir>/.test-reverse-index."""
    d = repo / ".git" / ".test-reverse-index"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_changed_lib_maps_to_sourcing_scripts_test(repo: Path) -> None:
    # base.sh has no test of its own; it is sourced by consumer.sh, which does.
    # Via the graph the lib maps to the consumer's mirror test.
    _write(repo, "tests/unit/test_consumer.py", 'HOOK = HOOKS / "consumer.sh"\n')
    _write(repo, "shared/hooks/consumer.sh", '#!/bin/sh\nsource "$D/lib/base.sh"\n')
    _write(repo, "shared/hooks/lib/base.sh", "#!/bin/sh\n")
    _commit_all(repo)

    assert _lookup(repo, "shared/hooks/lib/base.sh") == ["tests/unit/test_consumer.py"]


def test_changed_lib_maps_transitively_through_the_graph(repo: Path) -> None:
    # base.sh <- mid.sh <- top.sh; only top.sh has a test. A base.sh change must
    # reach top.sh's mirror test through two source hops.
    _write(repo, "tests/unit/test_top.py", 'HOOK = "top.sh"\n')
    _write(repo, "shared/hooks/top.sh", '#!/bin/sh\nsource "$D/mid.sh"\n')
    _write(repo, "shared/hooks/mid.sh", '#!/bin/sh\nsource "$D/lib/base.sh"\n')
    _write(repo, "shared/hooks/lib/base.sh", "#!/bin/sh\n")
    _commit_all(repo)

    assert _lookup(repo, "shared/hooks/lib/base.sh") == ["tests/unit/test_top.py"]


def test_changed_lib_unions_direct_and_graph_tests(repo: Path) -> None:
    # base.sh has its OWN test AND is sourced by a separately-tested consumer;
    # the lookup returns both, sorted-unique.
    _write(repo, "tests/unit/test_base.py", 'HOOK = "base.sh"\n')
    _write(repo, "tests/unit/test_consumer.py", 'HOOK = "consumer.sh"\n')
    _write(repo, "shared/hooks/consumer.sh", '#!/bin/sh\nsource "$D/lib/base.sh"\n')
    _write(repo, "shared/hooks/lib/base.sh", "#!/bin/sh\n")
    _commit_all(repo)

    assert _lookup(repo, "shared/hooks/lib/base.sh") == [
        "tests/unit/test_base.py",
        "tests/unit/test_consumer.py",
    ]


def test_leaf_script_change_still_maps_to_own_test(repo: Path) -> None:
    # AC2 regression: a leaf that sources nothing and is sourced by nothing still
    # maps to its own mirror test — the graph adds nothing, removes nothing.
    _write(repo, "tests/unit/test_leaf.py", 'HOOK = "leaf.sh"\n')
    _write(repo, "scripts/leaf.sh", "#!/bin/sh\necho hi\n")
    _commit_all(repo)

    assert _lookup(repo, "scripts/leaf.sh") == ["tests/unit/test_leaf.py"]


def test_var_source_without_sh_token_adds_no_edge(repo: Path) -> None:
    # A `source "$VAR"` with no resolvable .sh basename yields no graph edge:
    # under-map (safe, backstopped by the sweep), never a fabricated mapping.
    _write(repo, "tests/unit/test_consumer.py", 'HOOK = "consumer.sh"\n')
    _write(repo, "shared/hooks/consumer.sh", '#!/bin/sh\nsource "$DYNAMIC"\n')
    _write(repo, "shared/hooks/lib/base.sh", "#!/bin/sh\n")
    _commit_all(repo)

    assert _lookup(repo, "shared/hooks/lib/base.sh") == []


def test_committed_sh_change_mints_new_cache_key(repo: Path) -> None:
    # AC4: the cache invalidates on a committed .sh change — the graph depends on
    # shell state, so a new key is minted. Observed via the cache dir, without
    # hard-coding the key formula.
    _write(repo, "tests/unit/test_consumer.py", 'HOOK = "consumer.sh"\n')
    _write(repo, "shared/hooks/consumer.sh", '#!/bin/sh\nsource "$D/lib/base.sh"\n')
    _write(repo, "shared/hooks/lib/base.sh", "#!/bin/sh\n")
    _commit_all(repo)
    _lookup(repo, "shared/hooks/lib/base.sh")
    before = _cache_keys(repo)

    _write(repo, "shared/hooks/lib/base.sh", "#!/bin/sh\n# edit a sourced lib\n")
    _commit_all(repo, "chore: edit a sourced lib")
    _lookup(repo, "shared/hooks/lib/base.sh")

    assert set(_cache_keys(repo)) - set(before)  # a new cache key appeared
