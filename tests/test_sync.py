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
    git(root / "v2", "init", "-q")
    git(root / "v2", "remote", "add", "origin", "git@github.com:acme/toolkit.git")
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
    # every rule, the guidelines too, loads from its own subfolder (no paths: = always on, paths: = conditional); the project's CLAUDE.md is never touched
    assert sorted(p.name for p in (target / ".claude" / "rules").iterdir()) == ["ai-toolkit"]
    assert sorted(p.name for p in (target / ".claude" / "rules" / "ai-toolkit").iterdir()) == ["guidelines.md", "python-style.md", "security.md"]
    assert (target / ".claude" / "rules" / "ai-toolkit" / "guidelines.md").read_text() == '---\ndescription: "g"\n---\n# Guidelines\n'
    assert not (target / "CLAUDE.md").exists() and not (target / "CLAUDE.md.bak").exists()
    assert (target / ".ai-toolkit" / "rules" / "workflow.md").is_file()  # on-demand: never auto-loaded
    assert (target / ".claude" / "skills" / "land" / "references" / "r.md").is_file()
    assert (target / ".claude" / "agents" / "debug.md").is_file()
    assert (target / ".claude" / "commands" / "commit-msg.md").is_file()
    assert (target / ".ai-toolkit" / "claude-settings.json").read_text() == '{"hooks": {}}\n'   # the toolkit's own registrations; the project's .claude/settings.json is never written
    assert not (target / ".claude" / "settings.json").exists()
    for p in ("scripts/sync.sh", "scripts/lib.sh", "bin/claude-spoke", "hooks/git/commit-msg", "ai-toolkit.env"):
        assert (target / ".ai-toolkit" / p).is_file(), p
    # the hook scripts are the toolkit's, not the project's .claude/: a worker runs the main checkout's copy; modes survive the copy
    assert os.access(target / ".ai-toolkit" / "hooks" / "claude" / "guard.sh", os.X_OK)
    assert os.access(target / ".ai-toolkit" / "hooks" / "git" / "commit-msg", os.X_OK)
    assert not (target / ".claude" / "hooks").exists()
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
    assert {".claude/rules/ai-toolkit/guidelines.md", "orca.yaml", ".ai-toolkit/claude-settings.json", ".claude/skills/land/SKILL.md"} <= set(m) and "CLAUDE.md" not in m
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
    rule = target / ".claude" / "rules" / "ai-toolkit" / "guidelines.md"
    rule.chmod(0o640)   # the source is a 0600 temp file: the existing mode must survive an update
    real = write(tmp_path / "elsewhere.json", "{}\n")
    (target / ".ai-toolkit" / "claude-settings.json").unlink()
    (target / ".ai-toolkit" / "claude-settings.json").symlink_to(real)
    write(src / "shared" / "rules" / "guidelines.md", "---\ndescription: g\n---\n# Changed\n")
    write(src / "v2" / "settings" / "claude" / "settings.json", '{"hooks": {"x": 1}}\n')
    sync()
    assert rule.read_text() == "---\ndescription: g\n---\n# Changed\n" and rule.stat().st_mode & 0o777 == 0o640
    assert (target / ".ai-toolkit" / "claude-settings.json").is_symlink() and real.read_text() == '{"hooks": {"x": 1}}\n'


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


def test_every_sync_excludes_what_it_wrote_under_claude_and_only_local_only_also_orca_yaml(sync, target):
    write(target / ".git" / "info" / "exclude", "mine\n")
    sync("--local-only")
    sync("--local-only")
    ex = exclude(target)
    assert ex.count("# >>> ai-toolkit sync") == 1 and ex.startswith("mine\n")
    assert {"/.ai-toolkit/", "/.claude/skills/land/SKILL.md", "/.claude/rules/ai-toolkit/security.md", "/orca.yaml"} <= set(ex.splitlines())
    sync()
    lines = exclude(target).splitlines()   # a normal run keeps the .claude/ paths it wrote, one by one (never the whole folder), and drops orca.yaml
    assert {"/.ai-toolkit/", "/.claude/skills/land/SKILL.md", "mine"} <= set(lines) and not {"/.claude/", "/CLAUDE.md", "/orca.yaml"} & set(lines)


def test_local_env_override_is_never_tracked(sync, target):
    sync()
    assert git(target, "status", "--porcelain", "-uall").splitlines() == ["?? orca.yaml"]   # .claude/ and .ai-toolkit/ stay out of git; only orca.yaml is the project's to commit
    write(target / ".ai-toolkit" / "ai-toolkit.local.env", "BASE_BRANCH=dev\n")
    assert "ai-toolkit.local.env" not in git(target, "status", "--porcelain", "-uall")


def test_owner_repo_reads_every_remote_form_and_refuses_what_is_no_repo(src, run):
    forms = ["git@github.com:acme/toolkit.git", "https://github.com/acme/toolkit.git", "https://github.com/acme/toolkit",
             "ssh://git@github.com/acme/toolkit.git", "https://github.com/acme/toolkit/", "../a/b", "/srv/toolkit", ""]
    probe = f'. "{src}/v2/scripts/sync.sh"; for u in "$@"; do owner_repo "$u" || printf -; echo; done'
    r = run(["bash", "-c", probe, "_", *forms])
    assert r.stdout.splitlines() == ["acme/toolkit"] * 5 + ["-"] * 3  # one bash for every form: no full sync per URL


def test_a_host_records_the_toolkits_origin_as_upstream_and_the_toolkit_itself_stays_empty(sync, src, target, run):
    def value():
        return [ln for ln in (target / ".ai-toolkit" / "ai-toolkit.env").read_text().splitlines() if ln.startswith("UPSTREAM_REPO=")]
    assert sync().returncode == 0 and value() == ["UPSTREAM_REPO=acme/toolkit"]
    write(target / ".ai-toolkit" / "ai-toolkit.local.env", "UPSTREAM_REPO=me/fork\n")  # the per-target override still wins
    probe = f'. "{target}/.ai-toolkit/scripts/lib.sh"; ORCA_ROOT_PATH="{target}" load_env; echo "$UPSTREAM_REPO"'
    assert run(["bash", "-c", probe]).stdout.strip() == "me/fork"
    assert sync(tgt=src / "v2").returncode == 0  # the toolkit syncing into itself: nothing to point at
    assert (src / "v2" / ".ai-toolkit" / "ai-toolkit.env").read_text() == (src / "v2" / "settings" / "ai-toolkit.env").read_text()
    git(src / "v2", "remote", "remove", "origin")
    r = sync()
    assert r.returncode == 0 and r.stderr.count("UPSTREAM_REPO") == 1 and value() == ["UPSTREAM_REPO="]


def test_an_unmanaged_orca_yaml_is_backed_up_once_then_owned(sync, target):
    write(target / "orca.yaml", "scripts: {}\n")
    sync()
    assert (target / "orca.yaml.bak").read_text() == "scripts: {}\n" and (target / "orca.yaml").read_text().startswith("setupAgentStartupPolicy")
    (target / "orca.yaml").write_text("edited after sync\n")
    sync()  # now managed: overwritten again, the original backup is kept
    assert (target / "orca.yaml.bak").read_text() == "scripts: {}\n" and (target / "orca.yaml").read_text().startswith("setupAgentStartupPolicy")


def test_missing_settings_json_warns_but_syncs(sync, src, target):
    (src / "v2" / "settings" / "claude" / "settings.json").unlink()
    r = sync()
    assert r.returncode == 0 and "settings.json" in r.stderr
    assert not (target / ".ai-toolkit" / "claude-settings.json").exists() and (target / "orca.yaml").exists()


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
    assert (target / ".ai-toolkit" / "hooks" / "claude" / "guard.sh").is_file()  # the v2 hook outlives the GC of v1 hooks
    assert (target / ".cursor" / "mine.json").read_text() == "user" and victim.read_text() == "keep"
    assert (target / ".ai-toolkit" / "claude-settings.json").is_file()  # the v2 output survives the GC


def test_otel_sh_stays_in_the_toolkit_checkout(sync, src, target):
    assert (src / "v2" / "scripts" / "otel.sh").is_file()  # the fixture copies the real scripts
    sync()
    assert not (target / ".ai-toolkit" / "scripts" / "otel.sh").exists() and "otel.sh" not in "".join(manifest(target))
    stale = write(target / ".ai-toolkit" / "scripts" / "otel.sh")  # shipped by an earlier sync
    with (target / ".ai-toolkit" / "sync-manifest").open("a") as f:
        f.write(".ai-toolkit/scripts/otel.sh\n")
    sync()
    assert not stale.exists()


def test_a_hosts_own_files_are_never_touched(sync, target):
    own = {"CLAUDE.md": "the project's\n", ".claude/rules/security.md": "its own security\n", ".claude/agents/debug.md": "its own agent\n",
           ".claude/settings.json": '{"hooks": {"mine": 1}}\n', ".claude/hooks/mine.sh": "#!/bin/sh\n"}
    for rel, text in own.items():
        write(target / rel, text)
    git(target, "add", "-f", ".claude/agents/debug.md", ".claude/settings.json")   # .claude/ files the project tracks itself
    r = sync()
    assert r.returncode == 0 and ".claude/agents/debug.md is tracked" in r.stderr
    assert {rel: (target / rel).read_text() for rel in own} == own and not list(target.rglob("*.bak"))   # byte for byte, no backup of what the sync never writes
    assert ".claude/agents/debug.md" not in manifest(target) and "/.claude/agents/debug.md" not in exclude(target).splitlines()
    assert (target / ".claude" / "rules" / "ai-toolkit" / "security.md").is_file() and (target / ".ai-toolkit" / "claude-settings.json").is_file()


@pytest.mark.parametrize("state", ["written", "with-bak", "not-the-syncs", "tracked", "toolkit-itself"])
def test_a_host_synced_the_old_way_is_cleaned_up_from_its_manifest_only(sync, src, target, state):
    target = src / "v2" if state == "toolkit-itself" else target   # the toolkit syncing itself: its .bak files are a v1 sync's output, never the project's own
    sync(tgt=target)
    own = ["CLAUDE.md", ".claude/settings.json"]   # the project's files the old sync took over (each kept as <file>.bak when it existed)
    old = {".claude/rules/security.md": "old flat\n", ".claude/hooks/guard.sh": "old hook\n"}   # toolkit files the old layout wrote
    for rel in own:
        write(target / rel, "mine\n" if state == "not-the-syncs" else "generated\n")
        if state in ("with-bak", "toolkit-itself"):
            write(target / f"{rel}.bak", "the project's\n")
    for rel, text in old.items():
        write(target / rel, text)
    with (target / ".ai-toolkit" / "sync-manifest").open("a") as f:   # what the old sync recorded
        f.write("".join(f"{rel}\n" for rel in [*old, *([] if state == "not-the-syncs" else own)]))
    if state == "tracked":
        git(target, "add", "-f", *own, *old, ".claude/skills/land/SKILL.md")   # the last one is a toolkit file the project committed
    r = sync(tgt=target)
    assert r.returncode == 0 and bool(list(target.rglob("*.bak"))) == (state == "toolkit-itself")   # a .bak is restored over its file, never left behind (but kept in the toolkit)
    if state == "tracked":   # the host's history is the human's: reported, never rewritten, no deletion committed
        assert all((target / rel).read_text() == "generated\n" and f"{rel} is tracked" in r.stderr for rel in own)
        assert all((target / rel).exists() and f"{rel} is tracked" in r.stderr for rel in old) and "CLAUDE.md.bak" in r.stderr
        assert r.stderr.count("land/SKILL.md is tracked") == 1   # one report per file, not one from the write and one from the clean-up
        return
    assert not any((target / rel).exists() for rel in old)
    want = {"written": None, "with-bak": "the project's\n", "not-the-syncs": "mine\n", "toolkit-itself": None}[state]
    assert [(target / rel).read_text() if (target / rel).exists() else None for rel in own] == [want, want]
