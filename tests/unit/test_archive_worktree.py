"""Unit tests for scripts/archive-worktree.sh (issue #362, Orca migration S3).

Orca runs the archive hook before removing a worktree and kills it at ~120 s (aborting the
removal), so the script only COPIES an OTel spoke's raw bodies + identity to a spool under
<git-common-dir>/ai-toolkit-afk/ingest-spool/<spoke_run_id>/ — seconds, no network, always
exit 0, idempotent, and a kill at any point never leaves the spool missing or half-built.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "archive-worktree.sh"
SPOKE_ID = "spoke-362-abc123"

_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
for _leak in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "ORCA_WORKTREE_PATH"):
    _ENV.pop(_leak, None)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "i")
    return r


@pytest.fixture()
def worktree(repo: Path, tmp_path: Path) -> Path:
    """A linked worktree carrying an OTel spoke's .ai-toolkit state."""
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    ait = wt / ".ai-toolkit"
    (ait / "raw-bodies").mkdir(parents=True)
    (ait / "raw-bodies" / "req-1.json").write_text('{"a":1}')
    (ait / "spoke-run-id").write_text(SPOKE_ID + "\n")
    (ait / "lane").write_text("spoke\n")
    (ait / "mode").write_text("afk\n")
    (ait / "identity").write_text("issue=362\n")
    return wt


def _spool_root(repo: Path) -> Path:
    return repo / ".git" / "ai-toolkit-afk" / "ingest-spool"


def _run(
    wt: Path | None, *, arg: bool = False, cwd: Path | None = None, **env: str
) -> subprocess.CompletedProcess[str]:
    full = {**_ENV, **env}
    if wt is not None and not arg:
        full["ORCA_WORKTREE_PATH"] = str(wt)
    argv = ["bash", str(SCRIPT)] + ([str(wt)] if arg and wt else [])
    return subprocess.run(
        argv, cwd=cwd or wt, env=full, capture_output=True, text=True, timeout=30, check=False
    )


def _tree_hash(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_spools_otel_spoke_mirroring_worktree_layout(repo: Path, worktree: Path) -> None:
    r = _run(worktree)
    assert r.returncode == 0, r.stderr
    spool = _spool_root(repo) / SPOKE_ID / ".ai-toolkit"
    assert (spool / "raw-bodies" / "req-1.json").read_text() == '{"a":1}'
    assert (spool / "spoke-run-id").read_text().strip() == SPOKE_ID
    for name in ("lane", "mode", "identity"):
        assert (spool / name).is_file()


def test_optional_files_copied_only_when_present(repo: Path, worktree: Path) -> None:
    (worktree / ".ai-toolkit" / "identity").unlink()
    (worktree / ".ai-toolkit" / "mode").unlink()
    assert _run(worktree).returncode == 0
    spool = _spool_root(repo) / SPOKE_ID / ".ai-toolkit"
    assert not (spool / "identity").exists()
    assert not (spool / "mode").exists()
    assert (spool / "lane").is_file()


def test_idempotent_identical_tree_and_no_siblings(repo: Path, worktree: Path) -> None:
    assert _run(worktree).returncode == 0
    first = _tree_hash(_spool_root(repo))
    assert _run(worktree).returncode == 0
    assert _tree_hash(_spool_root(repo)) == first
    assert [p.name for p in _spool_root(repo).iterdir()] == [SPOKE_ID]


def test_rerun_refreshes_spool_with_new_bodies(repo: Path, worktree: Path) -> None:
    assert _run(worktree).returncode == 0
    (worktree / ".ai-toolkit" / "raw-bodies" / "req-2.json").write_text("{}")
    assert _run(worktree).returncode == 0
    assert (_spool_root(repo) / SPOKE_ID / ".ai-toolkit" / "raw-bodies" / "req-2.json").is_file()


def test_stale_siblings_from_a_killed_run_are_removed(repo: Path, worktree: Path) -> None:
    assert _run(worktree).returncode == 0
    clean = _tree_hash(_spool_root(repo))
    root = _spool_root(repo)
    stale_tmp = root / f"{SPOKE_ID}.tmp.99999" / ".ai-toolkit"
    stale_tmp.mkdir(parents=True)
    (stale_tmp / "half-copied").write_text("x")
    stale_old = root / f"{SPOKE_ID}.old.99999" / ".ai-toolkit"
    stale_old.mkdir(parents=True)
    (stale_old / "spoke-run-id").write_text("old")
    assert _run(worktree).returncode == 0
    assert _tree_hash(root) == clean
    assert [p.name for p in root.iterdir()] == [SPOKE_ID]


def test_stale_siblings_with_no_final_converge(repo: Path, worktree: Path) -> None:
    """Killed between 'final -> .old' and 'tmp -> final': final is absent on re-run."""
    root = _spool_root(repo)
    stale_old = root / f"{SPOKE_ID}.old.99999" / ".ai-toolkit"
    stale_old.mkdir(parents=True)
    (stale_old / "spoke-run-id").write_text("old")
    assert _run(worktree).returncode == 0
    assert [p.name for p in root.iterdir()] == [SPOKE_ID]
    assert (root / SPOKE_ID / ".ai-toolkit" / "raw-bodies" / "req-1.json").is_file()


def test_final_spool_never_absent_during_swap() -> None:
    """The old spool is renamed aside, then the new one moved in, then the old deleted."""
    text = SCRIPT.read_text()
    aside = text.index('mv "$FINAL" "$OLD"')
    move_in = text.index('mv "$TMP" "$FINAL"')
    delete_old = text.index('rm -rf "$OLD"')
    assert aside < move_in < delete_old


def test_non_otel_worktree_spools_nothing(repo: Path, worktree: Path) -> None:
    import shutil

    shutil.rmtree(worktree / ".ai-toolkit" / "raw-bodies")
    r = _run(worktree)
    assert r.returncode == 0
    assert r.stderr == ""
    assert not _spool_root(repo).exists()


def test_no_ai_toolkit_dir_is_quiet_noop(repo: Path, worktree: Path) -> None:
    import shutil

    shutil.rmtree(worktree / ".ai-toolkit")
    r = _run(worktree)
    assert r.returncode == 0
    assert r.stderr == ""
    assert not _spool_root(repo).exists()


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_missing_or_empty_spoke_run_id_exits_zero_loudly(
    repo: Path, worktree: Path, content: str | None
) -> None:
    f = worktree / ".ai-toolkit" / "spoke-run-id"
    if content is None:
        f.unlink()
    else:
        f.write_text(content)
    r = _run(worktree)
    assert r.returncode == 0
    assert "spoke-run-id" in r.stderr
    assert not _spool_root(repo).exists()


def test_branch_style_spoke_run_id_flattens_slashes(repo: Path, worktree: Path) -> None:
    real_id = "feature/362-feat-orca-add-orca+1790910976"
    (worktree / ".ai-toolkit" / "spoke-run-id").write_text(real_id + "\n")
    assert _run(worktree).returncode == 0
    root = _spool_root(repo)
    assert [p.name for p in root.iterdir()] == ["feature__362-feat-orca-add-orca+1790910976"]
    spooled = root / "feature__362-feat-orca-add-orca+1790910976" / ".ai-toolkit" / "spoke-run-id"
    assert spooled.read_text().strip() == real_id


def test_unsafe_spoke_run_id_is_refused(repo: Path, worktree: Path) -> None:
    (worktree / ".ai-toolkit" / "spoke-run-id").write_text("../escape\n")
    r = _run(worktree)
    assert r.returncode == 0
    assert "spoke-run-id" in r.stderr
    assert not (repo / ".git" / "ai-toolkit-afk" / "escape").exists()


def test_unwritable_spool_root_exits_zero_with_stderr_only(repo: Path, worktree: Path) -> None:
    afk = repo / ".git" / "ai-toolkit-afk"
    afk.mkdir()
    afk.chmod(0o500)
    try:
        if os.access(afk, os.W_OK):
            pytest.skip("running as a user that ignores directory permissions")
        r = _run(worktree)
    finally:
        afk.chmod(0o700)
    assert r.returncode == 0
    assert r.stderr.strip() != ""
    assert r.stdout == ""


def test_worktree_from_positional_arg(repo: Path, worktree: Path, tmp_path: Path) -> None:
    r = _run(worktree, arg=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (_spool_root(repo) / SPOKE_ID / ".ai-toolkit" / "spoke-run-id").is_file()


def test_worktree_from_cwd_fallback(repo: Path, worktree: Path) -> None:
    r = subprocess.run(
        ["bash", str(SCRIPT)], cwd=worktree, env=_ENV, capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, r.stderr
    assert (_spool_root(repo) / SPOKE_ID).is_dir()


def test_spool_lands_in_common_dir_not_worktree_gitdir(repo: Path, worktree: Path) -> None:
    assert _run(worktree).returncode == 0
    assert (_spool_root(repo) / SPOKE_ID).is_dir()
    assert not list((repo / ".git" / "worktrees").rglob("ingest-spool"))


def test_non_git_directory_exits_zero(tmp_path: Path) -> None:
    wt = tmp_path / "plain"
    (wt / ".ai-toolkit" / "raw-bodies").mkdir(parents=True)
    (wt / ".ai-toolkit" / "spoke-run-id").write_text(SPOKE_ID)
    r = _run(wt)
    assert r.returncode == 0
    assert r.stderr.strip() != ""


def test_sibling_of_a_live_concurrent_run_is_preserved(repo: Path, worktree: Path) -> None:
    root = _spool_root(repo)
    live = subprocess.Popen(["sleep", "30"])
    try:
        in_flight = root / f"{SPOKE_ID}.tmp.{live.pid}" / ".ai-toolkit"
        in_flight.mkdir(parents=True)
        (in_flight / "half-copied").write_text("x")
        assert _run(worktree).returncode == 0
        assert (in_flight / "half-copied").is_file()
    finally:
        live.kill()
        live.wait()


def _break_raw_bodies(worktree: Path) -> Path:
    blocked = worktree / ".ai-toolkit" / "raw-bodies" / "req-1.json"
    blocked.chmod(0o000)
    return blocked


def test_failed_copy_restores_the_previous_spool_and_leaves_a_marker(
    repo: Path, worktree: Path
) -> None:
    root = _spool_root(repo)
    old = root / f"{SPOKE_ID}.old.99999" / ".ai-toolkit"
    old.mkdir(parents=True)
    (old / "spoke-run-id").write_text("previous")
    blocked = _break_raw_bodies(worktree)
    try:
        if os.access(blocked, os.R_OK):
            pytest.skip("running as a user that ignores file permissions")
        r = _run(worktree)
    finally:
        blocked.chmod(0o644)
    assert r.returncode == 0
    assert (root / SPOKE_ID / ".ai-toolkit" / "spoke-run-id").read_text() == "previous"
    assert "could not copy" in (root / f"{SPOKE_ID}.failed").read_text()


def test_successful_run_clears_an_earlier_failure_marker(repo: Path, worktree: Path) -> None:
    root = _spool_root(repo)
    root.mkdir(parents=True)
    (root / f"{SPOKE_ID}.failed").write_text("earlier failure")
    assert _run(worktree).returncode == 0
    assert [p.name for p in root.iterdir()] == [SPOKE_ID]


def test_completes_under_five_seconds_on_realistic_bodies(repo: Path, worktree: Path) -> None:
    bodies = worktree / ".ai-toolkit" / "raw-bodies"
    blob = "x" * 20_000
    for i in range(600):
        (bodies / f"req-{i}.json").write_text(blob)
    start = time.monotonic()
    r = _run(worktree)
    elapsed = time.monotonic() - start
    assert r.returncode == 0, r.stderr
    assert elapsed < 5.0
    assert len(list((_spool_root(repo) / SPOKE_ID / ".ai-toolkit" / "raw-bodies").iterdir())) == 600
