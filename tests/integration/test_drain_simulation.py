"""Drain end-to-end simulation harness (issue #314).

ONE fixed harness that drives the REAL /afk drain tick loop (`hub-afk.sh --once`) and
the REAL watchdog (`hub-watchdog.sh --once`) against a fake clock and scripted mock
spokes, then checks a once-declared registry of INVARIANTS — properties that hold in
any correct implementation, mirroring `shared/rules/afk-design-principles.md`.

Design (issue #314):
  1. Invariants, not steps. The test never asserts "tick N does X"; it asserts
     principle-level properties, and changes only when a PRINCIPLE changes.
  2. Assert over EXPLICIT records only. Two #300-principle-1 append-only actor
     records are the assertion surface: the #300 transition log (spoke lifecycle)
     and the watchdog intervention ledger ("did a detector fire?"). Never epochs,
     pane text, `_`-internals, or log-line greps.
  3. Black-box the drain through its existing seams: `AFK_NOW` (fake clock),
     `AFK_STATE_DIR` (state + log), the `*_CMD` / `WT_*` / `BATCH_PLAN` stubs, and a
     PATH-shadowed tmux/gh/ps/claude. A mock spoke is a real git worktree whose
     markers/transitions are scripted on the fake timeline — the real drain<->spoke
     contract (tags + the log).
  4. Scenarios are DATA. Each `fixtures/drain_scenarios/*.yaml` is initial state + a
     scripted timeline + the invariant it stresses. Adding coverage adds a file;
     the harness never changes. Every scenario also declares an optional `mutation`
     — a fault injected through a seam that must turn exactly its one invariant red
     (the AC5 negative control).

macOS-only, exactly like the unit hub-afk suite: the drain reads transcript mtimes
with BSD `stat -f %m` (#129). A non-C-locale / Linux CI job is #189/#194's remit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="hub-afk.sh requires BSD stat (-f %m) and the macOS tmux hub (#129)",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_SCRIPTS = REPO_ROOT / "shared" / "skills" / "hub" / "scripts"
HUB_AFK = HUB_SCRIPTS / "hub-afk.sh"
HUB_WATCHDOG = HUB_SCRIPTS / "hub-watchdog.sh"
TLOG_LIB = HUB_SCRIPTS / "transition-log.sh"
SCENARIO_DIR = Path(__file__).parent / "fixtures" / "drain_scenarios"

# A pane pid the fake tmux advertises; the scripted agent hangs off it so the #301
# ancestor-walk resolves (mirrors tests/unit/_gate_broker_support.py).
_PANE_PID = 4242
_AGENT_PID = 4243


# --------------------------------------------------------------------------- world


@dataclass
class Spoke:
    """A scripted mock spoke = a git worktree on an issue-numbered branch."""

    issue: int
    slug: str
    path: Path
    agent_alive: bool = True
    truth: dict = field(default_factory=dict)


@dataclass
class World:
    """The isolated, hermetic drain world for one scenario run."""

    root: Path
    main: Path = field(init=False)
    origin: Path = field(init=False)
    state_dir: Path = field(init=False)
    projects: Path = field(init=False)
    fake_bin: Path = field(init=False)
    ledger: Path = field(init=False)
    home: Path = field(init=False)
    spokes: dict[int, Spoke] = field(default_factory=dict)
    env_extra: dict[str, str] = field(default_factory=dict)
    dropped: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.main = self.root / "main"
        self.origin = self.root / "origin.git"
        self.state_dir = self.root / "state"
        self.projects = self.root / "projects"
        self.fake_bin = self.root / "bin"
        self.ledger = self.state_dir / "intervention-ledger.jsonl"
        self.home = self.root / "home"
        for d in (self.state_dir, self.projects, self.fake_bin, self.home):
            d.mkdir(parents=True, exist_ok=True)
        self._init_git()
        self._write_stubs()
        self._write_window_state()

    # -- git ----------------------------------------------------------------

    def _git(self, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=True,
            env=self._git_env(),
        )

    def _git_env(self) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX"))
        }
        env.update(
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@t",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@t",
        )
        return env

    def _init_git(self) -> None:
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        self.main.mkdir()
        self._git("init", "-q", cwd=self.main)
        self._git("commit", "-q", "--allow-empty", "-m", "init", cwd=self.main)
        self._git("branch", "-M", "main", cwd=self.main)
        self._git("remote", "add", "origin", str(self.origin), cwd=self.main)
        self._git("push", "-q", "origin", "main", cwd=self.main)

    # -- stubs --------------------------------------------------------------

    def _write_stubs(self) -> None:
        """Shadow every real external the tick would touch with a scripted stub."""
        b = self.fake_bin
        # tmux: list-panes maps each live spoke's pane to its worktree; the
        # submitting Enter appends a type:"user" record (the real submit's proof of
        # delivery); display-message advertises the pane pid; everything else no-ops.
        (b / "tmux").write_text(_TMUX_STUB.format(state=self.state_dir))
        # ps: only the exact #301 probe form is answered from a per-spoke table;
        # every other ps execs the real one (hub-afk reads other -o forms).
        (b / "ps").write_text(_PS_STUB.format(state=self.state_dir))
        # gh / claude: never reached with real args in a stubbed tick; no-op.
        (b / "gh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (b / "claude").write_text("#!/usr/bin/env bash\nexit 0\n")
        # sibling scripts.
        (b / "batch-plan.sh").write_text("#!/usr/bin/env bash\nexit 0\n")  # no dispatch
        (b / "worktree-new.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (b / "worktree-done.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        # worktree-land.sh stub: mimic the real lander's log records (landing ->
        # landed) then exit 0, so auto_land emits `reaped` on top — the faithful
        # #299 pushed->ready->landed->reaped chain. A scenario overrides it via a
        # `land:` knob (e.g. fail) for a mutation.
        (b / "worktree-land.sh").write_text(_LAND_STUB.format(tlog=TLOG_LIB))
        for f in b.iterdir():
            f.chmod(0o755)

    def _write_window_state(self) -> None:
        (self.state_dir / ".afk-state").write_text("drain\n")
        # A live heartbeat pid (this process) so the watchdog's supervisor-dead
        # detector never fires spuriously in a scenario not about it.
        (self.state_dir / ".afk-heartbeat").write_text(f"{os.getpid()} wake1\n")

    # -- spokes -------------------------------------------------------------

    def add_spoke(
        self, issue: int, slug: str, *, agent_alive: bool = True, truth: dict | None = None
    ) -> Spoke:
        path = self.root / f"spoke-{issue}"
        branch = f"{issue}-{slug}"
        self._git("worktree", "add", "-q", "-b", branch, str(path), "main", cwd=self.main)
        self._git("commit", "-q", "--allow-empty", "-m", "spoke work", cwd=path)
        spoke = Spoke(issue=issue, slug=slug, path=path, agent_alive=agent_alive, truth=truth or {})
        self.spokes[issue] = spoke
        self._sync_pane_state(spoke)
        return spoke

    def _sync_pane_state(self, spoke: Spoke) -> None:
        """Write the per-spoke ground-truth files the tmux/ps stubs read."""
        d = self.state_dir / "panes"
        d.mkdir(exist_ok=True)
        (d / f"{spoke.issue}.alive").write_text("1" if spoke.agent_alive else "0")
        (d / f"{spoke.issue}.path").write_text(str(spoke.path))
        pane = d / f"{spoke.issue}.pane.txt"
        if not pane.exists():
            pane.write_text("")

    # -- env / runners ------------------------------------------------------

    def env(self, now: int) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            PATH=f"{self.fake_bin}:{os.environ['PATH']}",
            HOME=str(self.home),
            AFK_NOW=str(now),
            AFK_STATE_DIR=str(self.state_dir),
            AFK_STATE=str(self.state_dir / ".afk-state"),
            AFK_HEARTBEAT=str(self.state_dir / ".afk-heartbeat"),
            CLAUDE_PROJECTS_DIR=str(self.projects),
            BATCH_PLAN=str(self.fake_bin / "batch-plan.sh"),
            WT_NEW=str(self.fake_bin / "worktree-new.sh"),
            WT_LAND=str(self.fake_bin / "worktree-land.sh"),
            WT_DONE=str(self.fake_bin / "worktree-done.sh"),
            HUB_WATCHDOG_LEDGER=str(self.ledger),
            AFK_AUTH_PROBE_CMD="true",
            AFK_NET_PROBE_CMD="true",
            AI_TOOLKIT_OTEL="0",
            AI_TOOLKIT_GH_LIFECYCLE_LABELS="0",
            AFK_REVIEW_GATE="0",
            HUB_WATCHDOG_FILE="0",
        )
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(k, None)
        env.update(self.env_extra)
        return env

    def run_drain(self, now: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(HUB_AFK), "--once"],
            cwd=str(self.main),
            capture_output=True,
            text=True,
            env=self.env(now),
        )

    # -- transition-log writes (the scripted spoke side) --------------------

    def tlog(
        self,
        issue: int,
        to: str,
        actor: str,
        cause: str,
        *,
        evidence: str = "",
        episode: str = "",
        now: int,
    ) -> None:
        """Append a spoke-side transition exactly as the real actor would.

        A mutation may `drop` a transition name: withholding a recorded state
        reintroduces the pre-#300 "inference over recorded state" world (principle
        1), which is how a fixed watchdog false-fire is reintroduced without editing
        the (out-of-scope) watchdog."""
        if to in self.dropped:
            return
        args = [str(issue), to, actor, cause]
        if evidence or episode:
            args.append(evidence)
        if episode:
            args.append(episode)
        quoted = " ".join(_shq(a) for a in args)
        subprocess.run(
            ["bash", "-c", f'. "{TLOG_LIB}"; afk_tlog_transition {quoted}'],
            env={**self.env(now), "AFK_STATE_DIR": str(self.state_dir)},
            check=True,
        )

    def git(self, issue: int, *args: str) -> None:
        self._git(*args, cwd=self.spokes[issue].path)

    def set_agent_alive(self, issue: int, alive: bool) -> None:
        self.spokes[issue].agent_alive = alive
        self._sync_pane_state(self.spokes[issue])

    # -- record readers (the assertion surface) -----------------------------

    def records(self, issue: int) -> list[dict]:
        f = self.state_dir / "transitions" / f"{issue}.jsonl"
        if not f.exists():
            return []
        return [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]

    def transitions(self, issue: int) -> list[dict]:
        return [r for r in self.records(issue) if r.get("kind") == "transition"]

    def events(self, issue: int) -> list[dict]:
        return [r for r in self.records(issue) if r.get("kind") == "event"]

    def fires(self, issue: int | None = None) -> list[dict]:
        if not self.ledger.exists():
            return []
        rows = [json.loads(ln) for ln in self.ledger.read_text().splitlines() if ln.strip()]
        if issue is None:
            return rows
        return [r for r in rows if str(r.get("issue")) == str(issue)]


def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


_TMUX_STUB = r"""#!/usr/bin/env bash
# Scripted tmux: reads per-spoke ground truth under {state}/panes/.
panes_dir="{state}/panes"
case "$1" in
  list-panes)
    i=1
    for pf in "$panes_dir"/*.path; do
      [ -f "$pf" ] || continue
      iss="$(basename "$pf" .path)"
      [ "$(cat "$panes_dir/$iss.alive" 2>/dev/null)" = "1" ] || continue
      printf 'afk:%s\t%s\n' "$i" "$(cat "$pf")"
      i=$((i+1))
    done
    ;;
  display-message) printf '%s\n' "4242" ;;
  capture-pane)
    # -t <target> is a worktree path in list-panes output; find its pane file.
    tgt=""
    while [ $# -gt 0 ]; do case "$1" in -t) shift; tgt="$1" ;; esac; shift; done
    for pf in "$panes_dir"/*.path; do
      [ -f "$pf" ] || continue
      if [ "$(cat "$pf")" = "$tgt" ]; then
        cat "$panes_dir/$(basename "$pf" .path).pane.txt" 2>/dev/null; break
      fi
    done
    ;;
  *) : ;;
esac
exit 0
"""

_PS_STUB = r"""#!/usr/bin/env bash
# #301 probe: answer only the exact -eo form; build the table from live spokes.
case "$*" in
  "-eo pid=,ppid=,comm=")
    printf '%s 1 -zsh\n' "4242"
    for af in "{state}"/panes/*.alive; do
      [ -f "$af" ] || continue
      [ "$(cat "$af")" = "1" ] || continue
      printf '%s %s claude\n' "4243" "4242"
      break
    done
    printf '999 1 /Applications/Other.app/Contents/MacOS/claude\n'
    ;;
  *) exec /bin/ps "$@" ;;
esac
"""

_LAND_STUB = r"""#!/usr/bin/env bash
# Mimic worktree-land.sh's #300 records: landing (intent-first) -> landed, then rc 0
# so the drain's auto_land stamps `reaped`. A scenario mutation can point WT_LAND at
# a failing variant instead. Honors AFK_LAND_STUB_FAIL=1 to simulate a stuck land.
issue="$1"
. "{tlog}"
afk_tlog_transition "$issue" landing worktree-land.sh "merging into default"
if [ "${{AFK_LAND_STUB_FAIL:-0}}" = "1" ]; then
  afk_tlog_transition "$issue" land_failed worktree-land.sh "merge conflict"
  exit 1
fi
afk_tlog_transition "$issue" landed worktree-land.sh "merged into default"
exit 0
"""


# --------------------------------------------------------------------- scenario ops


def _seed_transcript(world: World, issue: int) -> None:
    """A minimal parked transcript so extract_pending_question can read a question."""
    slug = world.spokes[issue].slug
    munged = _project_slug(world.spokes[issue].path)
    d = world.projects / munged
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "AskUserQuestion",
                            "input": {"questions": [{"question": f"Proceed with {slug}?"}]},
                        }
                    ]
                },
            }
        )
        + "\n"
    )


def _project_slug(path: Path) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _pane_text(world: World, issue: int, text: str) -> None:
    (world.state_dir / "panes" / f"{issue}.pane.txt").write_text(text)


# The declarative timeline verbs. Each mutates scripted ground truth (git tags, the
# transition log, pane/agent state) OR runs a real drain/watchdog tick.
def apply_step(world: World, step: dict, *, mutation: dict | None) -> None:
    now = int(step["t"])
    do = step.get("do")
    if do:
        assert "spoke" in step, f"step {step!r} has `do` but no `spoke`"
        _VERBS[do](world, int(step["spoke"]), now, step, mutation)
    for actor in step.get("run", []):
        if actor == "drain":
            world.run_drain(now)
        elif actor == "watchdog":
            _run_watchdog(world, now)
        else:  # pragma: no cover - guard
            raise ValueError(f"unknown actor {actor!r}")


def _run_watchdog(world: World, now: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HUB_WATCHDOG), "--once"],
        cwd=str(world.main),
        capture_output=True,
        text=True,
        env=world.env(now),
    )


def _v_park(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    _seed_transcript(world, issue)
    _pane_text(world, issue, "Do you want to proceed?\n1. Yes\n2. No")
    world.tlog(issue, "parked", "afk-notify-wake", "parked on a question", now=now)


def _v_push(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    world.tlog(issue, "pushing", "spoke-push.sh", "pushing branch", now=now)
    world.tlog(issue, "pushed", "spoke-push.sh", "pushed; gate green", now=now)


def _v_ready(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    world.git(issue, "tag", "-f", f"ready/{issue}")
    world.tlog(issue, "ready", "spoke-ready.sh", "ready marker at tip", now=now)


def _v_commit(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    world.git(issue, "commit", "-q", "--allow-empty", "-m", step.get("msg", "work"))


def _v_kill_agent(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    world.set_agent_alive(issue, False)


def _v_land_start(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    """The spoke's own land process records `landing` intent-first, then stalls
    (pane dies mid-land) — the #290 shape. The `landing` record is exactly what the
    watchdog reads to DEFER a dead-pane fire; a mutation that drops it reintroduces
    the pre-#300 epoch-inference that false-fired."""
    world.tlog(issue, "landing", "worktree-land.sh", "merging into default", now=now)


def _v_block(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    world.git(issue, "tag", "-f", f"blocked/{issue}")
    world.tlog(issue, "blocked", "spoke-ready.sh", "escalated to a human", now=now)


_VERBS = {
    "park": _v_park,
    "push": _v_push,
    "ready": _v_ready,
    "commit": _v_commit,
    "kill_agent": _v_kill_agent,
    "land_start": _v_land_start,
    "block": _v_block,
}


# --------------------------------------------------------------------- invariants


@dataclass
class Violation:
    invariant: str
    issue: int
    message: str


def _last_index(records: list[dict], **match) -> int:
    idx = -1
    for i, r in enumerate(records):
        if all(r.get(k) == v for k, v in match.items()):
            idx = i
    return idx


def inv_pushed_ready_lands(world: World, scenario: dict) -> list[Violation]:
    """I2 (#299): a spoke that reached `pushed` and `ready` eventually reaches a
    terminal (`landed`/`reaped`); it never silently stalls (principles 2, 3)."""
    out: list[Violation] = []
    for issue in _scenario_issues(scenario):
        states = [t["to"] for t in world.transitions(issue)]
        if "pushed" in states and "ready" in states and not ({"landed", "reaped"} & set(states)):
            out.append(
                Violation(
                    "I2",
                    issue,
                    f"#{issue} reached pushed+ready but never landed/reaped "
                    f"(states={states}) — a pushed, ready spoke that never lands "
                    f"is the #299 silent stall (principle 2: fail loud, never "
                    f"silently accept the worst case).",
                )
            )
    return out


def inv_landing_never_dead_pane(world: World, scenario: dict) -> list[Violation]:
    """I1 (#290/#301): a spoke the scenario declares is in a `landing`/`pushing`
    phase is NEVER fired dead-pane. The recorded intent-first phase transition is
    what the watchdog reads to defer the fire (principles 1, 4); reintroducing
    epoch-inference (dropping the recorded state) is what false-fired."""
    out: list[Violation] = []
    for issue in _scenario_issues(scenario):
        phase = _spoke_truth(scenario, issue).get("phase")
        if phase not in {"landing", "pushing"}:
            continue
        dead = [f for f in world.fires(issue) if f.get("condition") == "dead-pane"]
        if dead:
            out.append(
                Violation(
                    "I1",
                    issue,
                    f"#{issue} was fired dead-pane while in the '{phase}' phase "
                    f"({len(dead)} fire(s)) — a spoke in a recorded multi-minute "
                    f"phase is structurally not a dead pane (principle 4: probe the "
                    f"real process, never a stale epoch proxy).",
                )
            )
    return out


# The fixed registry: declared ONCE, checked against EVERY scenario. Adding a
# scenario never touches this list; only a change to an AFK Design PRINCIPLE does.
INVARIANTS = [
    inv_pushed_ready_lands,
    inv_landing_never_dead_pane,
]


def _spoke_truth(scenario: dict, issue: int) -> dict:
    for s in scenario["spokes"]:
        if int(s["issue"]) == issue:
            return s.get("truth", {})
    return {}


def _scenario_issues(scenario: dict) -> list[int]:
    return [int(s["issue"]) for s in scenario["spokes"]]


def check_all(world: World, scenario: dict) -> list[Violation]:
    out: list[Violation] = []
    for inv in INVARIANTS:
        out.extend(inv(world, scenario))
    return out


# --------------------------------------------------------------------- the harness


def _build_world(root: Path, scenario: dict, *, mutation: dict | None = None) -> World:
    world = World(root=root)
    world.env_extra.update(scenario.get("env", {}))
    if mutation:
        _apply_mutation_env(world, mutation)
    for s in scenario["spokes"]:
        world.add_spoke(
            int(s["issue"]),
            s.get("slug", "feat"),
            agent_alive=s.get("initial", {}).get("agent_alive", True),
            truth=s.get("truth", {}),
        )
    return world


def _apply_mutation_env(world: World, mutation: dict) -> None:
    """Translate a scenario's `mutation` knobs into seam overrides. A mutation
    reintroduces a bug's PROXIMATE CAUSE through a stub/seam only — never by editing
    product code — so exactly one invariant must redden (the AC5 negative control)."""
    if mutation.get("land_fail"):
        world.env_extra["AFK_LAND_STUB_FAIL"] = "1"
    world.dropped.update(mutation.get("drop", []))
    world.env_extra.update(mutation.get("env", {}))


def _run_timeline(world: World, scenario: dict, *, mutation: dict | None) -> None:
    for step in sorted(scenario["timeline"], key=lambda s: int(s["t"])):
        apply_step(world, step, mutation=mutation)


def _load_scenarios() -> list[dict]:
    files = sorted(SCENARIO_DIR.glob("*.yaml"))
    return [yaml.safe_load(f.read_text()) | {"__file__": f.name} for f in files]


def _scenario_id(scenario: dict) -> str:
    return scenario.get("id", scenario.get("__file__", "?"))


_SCENARIOS = _load_scenarios()


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[_scenario_id(s) for s in _SCENARIOS])
def test_scenario_holds_invariants(tmp_path: Path, scenario: dict) -> None:
    """Every scenario's REAL run satisfies every invariant (green against real code)."""
    world = _build_world(tmp_path, scenario)
    _run_timeline(world, scenario, mutation=None)
    violations = check_all(world, scenario)
    expected = scenario.get("expect", {}).get("violations", [])
    got = sorted({v.invariant for v in violations})
    assert got == sorted(expected), (
        f"{_scenario_id(scenario)}: expected violations {sorted(expected)}, got "
        + "; ".join(f"{v.invariant}: {v.message}" for v in violations)
    )


_MUTATION_SCENARIOS = [s for s in _SCENARIOS if s.get("mutation")]


@pytest.mark.parametrize(
    "scenario", _MUTATION_SCENARIOS, ids=[_scenario_id(s) for s in _MUTATION_SCENARIOS]
)
def test_mutation_reddens_exactly_one_invariant(tmp_path: Path, scenario: dict) -> None:
    """AC5 negative control: reintroducing the bug through a seam turns exactly the
    scenario's one named invariant red — proving the invariant actually bites and a
    deliberate regression does not pass silently."""
    mutation = scenario["mutation"]
    want = mutation["expect_violation"]
    world = _build_world(tmp_path, scenario, mutation=mutation)
    _run_timeline(world, scenario, mutation=mutation)
    got = sorted({v.invariant for v in check_all(world, scenario)})
    assert got == [want], (
        f"{_scenario_id(scenario)}: mutation must redden exactly [{want}], got {got}"
    )
