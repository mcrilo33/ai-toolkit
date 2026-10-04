import json
import os
import shutil

import pytest

from conftest import V2, git


def write(path, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def src(tmp_path):
    """A fake toolkit checkout: the real sync/lib scripts around a tiny shared/ tree."""
    root = tmp_path / "src"
    shutil.copytree(V2 / "scripts", root / "v2" / "scripts")
    shutil.copytree(V2 / "bin", root / "v2" / "bin")
    (root / "v2" / "settings").mkdir(parents=True)
    shutil.copy(V2 / "settings" / "ai-toolkit.env", root / "v2" / "settings")
    write(root / "v2" / "settings" / "claude" / "settings.json", '{"hooks": {}}\n')
    write(root / "v2" / "hooks" / "claude" / "guard.sh", "#!/bin/sh\n").chmod(0o755)
    write(root / "v2" / "hooks" / "git" / "commit-msg", "#!/bin/sh\n").chmod(0o755)
    sh = root / "shared"
    write(sh / "rules" / "guidelines.md", '---\ndescription: "g"\n---\n# Guidelines\n')
    write(sh / "rules" / "security.md", "# Security\n")
    write(sh / "rules" / "python-style.md", "---\npaths:\n  - '**/*.py'\n---\n# Py\n")
    write(sh / "rules" / "on-demand" / "workflow.md", "# Workflow\n")
    write(sh / "skills" / "land" / "SKILL.md", "---\nname: land\ndescription: d\n---\n")
    write(sh / "skills" / "land" / "references" / "r.md")
    write(sh / "skills" / "land" / "scripts" / "__pycache__" / "x.pyc")
    write(sh / "skills" / "land" / ".DS_Store")
    write(sh / "skills" / "hub" / "SKILL.md", "---\nname: hub\ndescription: d\n---\n")
    write(sh / "agents" / "debug.md", "---\nname: debug\n---\n")
    write(sh / "prompts" / "commit-msg.md", "---\ndescription: d\n---\n")
    return root


@pytest.fixture
def target(tmp_path):
    t = tmp_path / "target"
    t.mkdir()
    git(t, "init", "-q")
    return t


@pytest.fixture
def sync(src, target, run):
    return lambda *flags, tgt=None: run(["bash", str(src / "v2" / "scripts" / "sync.sh"), str(tgt or target), *flags])


def tree(t):
    return {str(p.relative_to(t)): (p.read_bytes(), p.stat().st_mtime_ns) for p in t.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(t).parts}


def manifest(t):
    return (t / ".ai-toolkit" / "sync-manifest").read_text().splitlines()


def exclude(t):
    return (t / ".git" / "info" / "exclude").read_text()


def test_layout_in_target(sync, target):
    r = sync()
    assert r.returncode == 0, r.stderr
    assert (target / "CLAUDE.md").read_text() == "# Guidelines\n"  # frontmatter stripped
    # always-on (no paths:) and conditional rules both load from .claude/rules; guidelines is CLAUDE.md
    assert sorted(p.name for p in (target / ".claude" / "rules").iterdir()) == ["python-style.md", "security.md"]
    assert (target / ".ai-toolkit" / "rules" / "workflow.md").is_file()  # on-demand: never auto-loaded
    assert (target / ".claude" / "skills" / "land" / "references" / "r.md").is_file()
    assert (target / ".claude" / "agents" / "debug.md").is_file()
    assert (target / ".claude" / "commands" / "commit-msg.md").is_file()
    assert (target / ".claude" / "settings.json").read_text() == '{"hooks": {}}\n'
    for p in ("scripts/sync.sh", "scripts/lib.sh", "bin/claude-spoke", "hooks/git/commit-msg", "ai-toolkit.env"):
        assert (target / ".ai-toolkit" / p).is_file(), p
    # Claude hooks ship under .claude/ (setup.sh copies only .claude/ into a new worktree); modes survive the copy
    assert os.access(target / ".claude" / "hooks" / "guard.sh", os.X_OK)
    assert os.access(target / ".ai-toolkit" / "hooks" / "git" / "commit-msg", os.X_OK)
    assert not (target / ".ai-toolkit" / "hooks" / "claude").exists()
    assert not list(target.rglob("__pycache__")) and not list(target.rglob(".DS_Store"))
    assert not (target / ".claude" / "prompts").exists() and not (target / ".cursor").exists()


def test_orca_yaml_runs_setup_from_root_path(sync, target):
    sync()
    y = (target / "orca.yaml").read_text()
    assert "setupAgentStartupPolicy: wait-for-setup" in y
    assert 'setup: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/setup.sh"' in y
    assert 'archive: bash "$ORCA_ROOT_PATH/.ai-toolkit/scripts/archive.sh"' in y


def test_manifest_is_sorted_relative_and_excludes_itself(sync, target):
    sync()
    m = manifest(target)
    assert m == sorted(set(m))
    assert {"CLAUDE.md", "orca.yaml", ".claude/settings.json", ".claude/skills/land/SKILL.md"} <= set(m)
    assert ".ai-toolkit/sync-manifest" not in m and all(not p.startswith("/") for p in m)


def test_second_run_changes_nothing(sync, target):
    sync()
    before = tree(target)
    assert sync().returncode == 0
    assert tree(target) == before  # same bytes AND no rewrite (mtime) churn


def test_edit_in_source_reaches_target_by_rename_so_a_running_script_is_not_corrupted(sync, src, target):
    sync()
    write(src / "shared" / "skills" / "land" / "SKILL.md", "new\n")
    source = src / "v2" / "scripts" / "coordinator.sh"
    installed = target / ".ai-toolkit" / "scripts" / "coordinator.sh"
    old, inode = installed.read_text(), installed.stat().st_ino
    with installed.open() as running:   # a running bash holds its script open and reads it by offset
        source.write_text("#!/bin/sh\nnew\n")
        sync()
        assert running.read() == old   # the old inode is untouched: an in-place write would have changed what the loop reads next
    assert installed.read_text() == "#!/bin/sh\nnew\n" and installed.stat().st_ino != inode
    assert (target / ".claude" / "skills" / "land" / "SKILL.md").read_text() == "new\n"
    assert not [p for p in target.rglob("*.new.*")]


def test_a_rewrite_keeps_the_destinations_mode_and_writes_through_a_symlink(sync, src, target, tmp_path):
    sync()
    (target / "CLAUDE.md").chmod(0o644)   # the source is a 0600 temp file: the existing mode must survive an update
    real = write(tmp_path / "elsewhere.json", "{}\n")
    (target / ".claude" / "settings.json").unlink()
    (target / ".claude" / "settings.json").symlink_to(real)
    write(src / "shared" / "rules" / "guidelines.md", "---\ndescription: g\n---\n# Changed\n")
    write(src / "v2" / "settings" / "claude" / "settings.json", '{"hooks": {"x": 1}}\n')
    sync()
    assert (target / "CLAUDE.md").read_text() == "# Changed\n" and (target / "CLAUDE.md").stat().st_mode & 0o777 == 0o644
    assert (target / ".claude" / "settings.json").is_symlink() and real.read_text() == '{"hooks": {"x": 1}}\n'


def test_gc_removes_what_a_previous_sync_wrote_and_nothing_else(sync, src, target):
    sync()
    mine = write(target / ".claude" / "skills" / "hub" / "mine.md", "user file")
    other = write(target / ".claude" / "rules" / "mine.md", "user rule")
    shutil.rmtree(src / "shared" / "skills" / "land")
    (src / "shared" / "rules" / "security.md").unlink()
    assert sync().returncode == 0
    assert not (target / ".claude" / "skills" / "land").exists()  # file gone, empty dirs pruned
    assert not (target / ".claude" / "rules" / "security.md").exists()
    assert mine.read_text() == "user file" and other.read_text() == "user rule"
    assert (target / ".claude" / "skills" / "hub" / "SKILL.md").is_file()


def test_gc_ignores_manifest_entries_that_escape_the_target(sync, target, tmp_path):
    sync()
    victim = write(tmp_path / "victim.txt", "keep")
    with (target / ".ai-toolkit" / "sync-manifest").open("a") as f:
        f.write("../victim.txt\n/etc/hosts\n")
    sync()
    assert victim.read_text() == "keep"


def test_local_only_excludes_deployment_and_normal_run_undoes_it(sync, target):
    write(target / ".git" / "info" / "exclude", "mine\n")
    sync("--local-only")
    sync("--local-only")
    ex = exclude(target)
    assert ex.count("# >>> ai-toolkit sync") == 1 and ex.startswith("mine\n")
    assert {"/.ai-toolkit/", "/.claude/", "/CLAUDE.md", "/orca.yaml"} <= set(ex.splitlines())
    sync()
    ex = exclude(target)
    assert "/.ai-toolkit/" in ex.splitlines() and "mine" in ex.splitlines()
    assert not {"/.claude/", "/CLAUDE.md", "/orca.yaml"} & set(ex.splitlines())


def test_local_env_override_is_never_tracked(sync, target):
    sync()
    write(target / ".ai-toolkit" / "ai-toolkit.local.env", "BASE_BRANCH=dev\n")
    assert "ai-toolkit.local.env" not in git(target, "status", "--porcelain", "-uall")


def test_unmanaged_singletons_are_backed_up_once_then_owned(sync, target):
    write(target / "CLAUDE.md", "hand written\n")
    write(target / "orca.yaml", "scripts: {}\n")
    sync()
    assert (target / "CLAUDE.md.bak").read_text() == "hand written\n"
    assert (target / "orca.yaml.bak").read_text() == "scripts: {}\n"
    assert (target / "CLAUDE.md").read_text() == "# Guidelines\n"
    (target / "CLAUDE.md").write_text("edited after sync\n")
    sync()  # now managed: overwritten again, the original backup is kept
    assert (target / "CLAUDE.md.bak").read_text() == "hand written\n"


def test_missing_settings_json_warns_but_syncs(sync, src, target):
    (src / "v2" / "settings" / "claude" / "settings.json").unlink()
    r = sync()
    assert r.returncode == 0 and "settings.json" in r.stderr
    assert not (target / ".claude" / "settings.json").exists() and (target / "CLAUDE.md").exists()


def test_refuses_a_target_that_is_not_a_git_repo(sync, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = sync(tgt=plain)
    assert r.returncode != 0 and not (plain / ".claude").exists()


def test_rejects_unknown_option(sync):
    assert sync("--bogus").returncode != 0


def v1_target(target):
    """A target a v1 sync filled: manifest.json lists Cursor/Copilot/Claude outputs."""
    listed = [".cursor/rules/a.mdc", ".github/agents/x.md", ".claude/hooks/scripts/old.sh", "../victim.txt"]
    for rel in listed[:3]:
        write(target / rel)
    write(target / ".cursor" / "mine.json", "user")
    write(target / ".ai-toolkit-manifest.json", json.dumps({"toolkit_rev": "x", "tools": {
        "cursor": listed[:1], "copilot": listed[1:2], "claude": listed[2:]}}))


def test_v1_files_are_kept_unless_migrating_and_a_hint_is_printed(sync, target):
    v1_target(target)
    r = sync()
    assert (target / ".cursor" / "rules" / "a.mdc").exists() and "--migrate-v1" in r.stderr


def test_migrate_v1_removes_what_the_v1_manifest_listed_and_nothing_else(sync, target, tmp_path):
    v1_target(target)
    victim = write(tmp_path / "victim.txt", "keep")
    assert sync("--migrate-v1").returncode == 0
    assert not (target / ".cursor" / "rules").exists() and not (target / ".github").exists()
    assert not (target / ".claude" / "hooks" / "scripts").exists() and not (target / ".ai-toolkit-manifest.json").exists()
    assert (target / ".claude" / "hooks" / "guard.sh").is_file()  # the v2 hook outlives the GC of v1 hooks
    assert (target / ".cursor" / "mine.json").read_text() == "user" and victim.read_text() == "keep"
    assert (target / ".claude" / "settings.json").is_file()  # the v2 output survives the GC


def test_otel_sh_stays_in_the_toolkit_checkout(sync, src, target):
    assert (src / "v2" / "scripts" / "otel.sh").is_file()  # the fixture copies the real scripts
    sync()
    assert not (target / ".ai-toolkit" / "scripts" / "otel.sh").exists() and "otel.sh" not in "".join(manifest(target))
    stale = write(target / ".ai-toolkit" / "scripts" / "otel.sh")  # shipped by an earlier sync
    with (target / ".ai-toolkit" / "sync-manifest").open("a") as f:
        f.write(".ai-toolkit/scripts/otel.sh\n")
    sync()
    assert not stale.exists()
