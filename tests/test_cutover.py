"""cutover.sh (06 section 8): the single v1 -> v2 restructuring commit, on a synthetic repo."""
import re
import shutil
import subprocess

import pytest
from conftest import ROOT, V2, git

V1 = ["scripts/old.sh", "scripts/telemetry/t.py", "shared/hooks/h.sh", "shared/skills/hub/scripts/x.sh", "shared/skills/hub/SKILL.md",
      "shared/skills/source-task/SKILL.md", "shared/skills/solo/SKILL.md", "shared/rules/metadata.yml", "shared/rules/a.md", "shared/pyproject.toml",
      "mcp/m.py", "dashboard/d.json", "settings/ai-toolkit.yml", "settings/cursor/c.json", "tests/old_test.py", "docs/old.md", "docs/orca-migration/x.md",
      "CLAUDE.md", "README.md", "orca.yaml", "pyproject.toml", "ruff.toml", ".test-select-exempt", ".gitignore", ".github/workflows/ci.yml", "VERSION"]
NEW = ["v2/scripts/new.sh", "v2/settings/ai-toolkit.env", "v2/orca.yaml", "v2/README.md", "v2/bin/b", "v2/hooks/h", "v2/.shellcheckrc",
       "v2/.github/workflows/ci.yml", "v2/.gitignore", "tests_v2/test_x.py", "tests_v2/pytest.ini", "docs/v2/architecture.md", "docs/v2/frontmatter.md", "docs/v2/wp1-notes.md"]
GONE = ["scripts/old.sh", "scripts/telemetry", "shared/hooks", "shared/skills/hub/scripts", "shared/skills/source-task", "shared/rules/metadata.yml",
        "shared/pyproject.toml", "mcp", "dashboard", "settings/ai-toolkit.yml", "settings/cursor", "tests/old_test.py", "docs/old.md", "docs/orca-migration",
        "docs/v2", "CLAUDE.md", "pyproject.toml", "ruff.toml", ".test-select-exempt", "v2", "tests_v2"]


def put(root, rel, text=None):
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text if text is not None else f"{rel}\n")


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """A v1 main (pushed to a bare origin) and a v2 branch on top of it, built once per module and copied per test."""
    base = tmp_path_factory.mktemp("tpl")
    origin, root = base / "origin.git", base / "clone"
    subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(root)], check=True, capture_output=True)
    git(root, "config", "user.name", "t"); git(root, "config", "user.email", "t@t")
    for rel in V1:
        put(root, rel)
    put(root, ".gitignore", "settings/*\n.claude/\n")  # v1's real .gitignore ignores settings/*: the moved v2 settings must stay tracked
    put(root, "requirements-dev.txt", "pytest>=8,<9\npytest-testmon>=2,<3\npytest-xdist>=3,<4\npyyaml>=6,<7\nduckdb>=1,<2\n")
    git(root, "add", "-A"); git(root, "commit", "-qm", "v1"); git(root, "push", "-q", "origin", "main")
    git(root, "checkout", "-qb", "v2")
    for rel in NEW:
        put(root, rel)
    git(root, "add", "-A"); git(root, "commit", "-qm", "v2 work")
    return base


@pytest.fixture
def cut(template, tmp_path, run):
    """(checkout, run-cutover): a private copy of the template, origin re-pointed at the copy of the bare repo."""
    shutil.copytree(template, tmp_path / "w", symlinks=True)
    root = tmp_path / "w" / "clone"
    git(root, "remote", "set-url", "origin", str(tmp_path / "w" / "origin.git"))
    return root, lambda *a: run(["bash", str(V2 / "scripts" / "cutover.sh"), *a], cwd=root)


def test_dry_run_lists_every_move_and_delete_and_touches_nothing(cut):
    root, go = cut
    before = git(root, "status", "--porcelain=v1", "--ignored"), git(root, "rev-parse", "HEAD")
    r = go("--dry-run")
    assert r.returncode == 0, r.stderr
    for p in ("tag v1-final", "delete scripts", "delete shared/hooks", "delete mcp", "delete tests", "move v2/scripts -> scripts",
              "move v2/.github -> .github", "move v2/.shellcheckrc -> .shellcheckrc", "move tests_v2 -> tests"):
        assert p in r.stdout
    assert (git(root, "status", "--porcelain=v1", "--ignored"), git(root, "rev-parse", "HEAD")) == before
    assert git(root, "tag") == ""


def test_cutover_makes_one_commit_with_the_v2_layout(cut):
    root, go = cut
    tip = git(root, "rev-parse", "HEAD")
    r = go()
    assert r.returncode == 0, r.stderr
    assert git(root, "rev-parse", "v1-final") == git(root, "rev-parse", "origin/main") != tip  # the v1 tip, not v2's
    assert git(root, "rev-list", "--count", f"{tip}..HEAD") == "1" and git(root, "status", "--porcelain") == ""
    for p in GONE:
        assert not (root / p).exists(), p
    for p in ("scripts/new.sh", "settings/ai-toolkit.env", "orca.yaml", "README.md", "bin/b", "hooks/h", ".shellcheckrc", ".github/workflows/ci.yml",
              "tests/test_x.py", "docs/architecture.md", "docs/frontmatter.md", "shared/rules/a.md", "shared/skills/solo/SKILL.md", "shared/skills/hub/SKILL.md", "VERSION", ".gitignore"):
        assert (root / p).is_file(), p
    tracked = set(git(root, "ls-files").splitlines())   # every file v2 tracked is still tracked at its new path, ignored or not
    assert {"settings/ai-toolkit.env", "scripts/new.sh", "tests/test_x.py", "docs/architecture.md", ".github/workflows/ci.yml"} <= tracked
    assert (root / "orca.yaml").read_text() == "v2/orca.yaml\n"  # replaced by the v2 file, not merged
    assert (root / "requirements-dev.txt").read_text().split() == ["pytest>=8,<9", "pytest-xdist>=3,<4", "pyyaml>=6,<7"]
    ignored = (root / ".gitignore").read_text().splitlines()
    assert "/CLAUDE.md" in ignored and "settings/*" not in ignored  # sync.sh generates CLAUDE.md; v1's ignore rules are replaced by v2's file


def test_rollback_is_a_reset_to_the_v1_final_tag(cut):
    root, go = cut
    assert go().returncode == 0
    git(root, "reset", "-q", "--hard", "v1-final")
    assert (root / "scripts" / "old.sh").is_file() and not (root / "v2").exists()


def test_tag_stays_local_unless_push(cut):
    root, go = cut
    assert go().returncode == 0
    assert git(root, "ls-remote", "--tags", "origin") == ""
    git(root, "reset", "-q", "--hard", "HEAD~1"); git(root, "tag", "-d", "v1-final")
    assert go("--push").returncode == 0
    assert "refs/tags/v1-final" in git(root, "ls-remote", "--tags", "origin")


@pytest.mark.parametrize("breakage", ["dirty", "no-v2", "tag-exists", "bad-ref"])
def test_refuses_and_changes_nothing(cut, breakage):
    root, go = cut
    args = ()
    if breakage == "dirty":
        put(root, "scripts/new-file.sh")
        git(root, "add", "scripts/new-file.sh")
    elif breakage == "no-v2":
        git(root, "rm", "-rq", "v2"); git(root, "commit", "-qm", "x")
    elif breakage == "tag-exists":
        git(root, "tag", "v1-final")
    else:
        args = ("--v1", "nope")
    before = git(root, "rev-parse", "HEAD")
    r = go(*args)
    assert r.returncode == 2 and r.stderr.strip()
    assert git(root, "rev-parse", "HEAD") == before and (root / "scripts" / "old.sh").is_file()


def test_sync_into_itself_generates_claude_md_and_keeps_the_tracked_orca_yaml(tmp_path, run):
    """After cutover the toolkit syncs into itself: CLAUDE.md is sync's output; the tracked root orca.yaml is not overwritten."""
    root = tmp_path / "toolkit"
    for d in ("scripts", "bin", "hooks", "settings"):
        shutil.copytree(V2 / d, root / d)
    put(root, "shared/rules/guidelines.md", "---\ndescription: g\n---\n# Guidelines\n")
    put(root, "orca.yaml", "setup: ./scripts/setup.sh\n")
    git(root, "init", "-q", "--initial-branch=main")
    r = run(["bash", str(root / "scripts" / "sync.sh"), str(root)])
    assert r.returncode == 0, r.stderr
    assert (root / "CLAUDE.md").read_text() == "# Guidelines\n"
    assert (root / "orca.yaml").read_text() == "setup: ./scripts/setup.sh\n" and not (root / "orca.yaml.bak").exists()
    assert (root / ".ai-toolkit" / "scripts" / "setup.sh").is_file()


def test_nothing_points_at_a_file_the_cutover_deletes():
    """docs/ keeps only architecture.md and v2/ moves to the root: no policy or script may name a deleted doc or the v2/ and tests_v2/ prefixes."""
    stale = re.compile(r"(?<![A-Za-z0-9_./-])docs/(?!(?:architecture|frontmatter)\.md)[A-Za-z0-9_./-]+\.md|shared/hooks/|\bscripts/(worktree|hub|spoke|sync-to-repo)")
    roots = [V2 / "scripts", V2 / "hooks", V2 / "bin", V2 / "e2e", ROOT / "shared"]
    hits = [f"{f.relative_to(ROOT)}: {m.group(0)}" for r in roots for f in r.rglob("*") if f.is_file() and f.suffix in {"", ".sh", ".md", ".py"} and not f.relative_to(ROOT).as_posix().startswith(("shared/hooks/", "shared/skills/hub/scripts/"))  # both deleted by cutover.sh
            for m in stale.finditer(f.read_text(errors="ignore")) if f.name != "cutover.sh"]
    assert not hits, hits
