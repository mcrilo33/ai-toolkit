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
import time
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

# Wall-clock cap per real drain/watchdog tick (see World.run_drain). Generous for
# legitimate stubbed work; cuts a post-answer poll that would otherwise wait out a
# ~30s timeout against a scripted spoke that never changes.
_DRAIN_TICK_CAP_SECONDS = 10


# --------------------------------------------------------------------------- world


@dataclass
class Spoke:
    """A scripted mock spoke = a git worktree on an issue-numbered branch."""

    issue: int
    slug: str
    path: Path
    agent_alive: bool = True
    pane_exists: bool = True
    truth: dict = field(default_factory=dict)
    episode: str = ""


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
    t0: int = field(init=False)

    def __post_init__(self) -> None:
        # Anchor the fake clock at real time: the drain/stubs stamp their own records
        # from real `date +%s`, so AFK_NOW(step) = t0 + step keeps scripted records,
        # epoch files, and drain-written records on one consistent timeline.
        self.t0 = int(time.time())
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
        # timeout: this host ships no coreutils timeout, so the drain's timeout wrapper
        # falls to a killer-subshell that holds a bounded command's capture pipe open for
        # the whole budget (30s per answerer call in CI). A stub that execs the command
        # directly restores the fast path — every bounded command here is an instant stub.
        (b / "timeout").write_text(
            "#!/usr/bin/env bash\n"
            'while [ "$1" = "-k" ]; do shift 2; done\n'  # drop -k <grace>
            "shift\n"  # drop the <secs> bound
            'exec "$@"\n'
        )
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
        """Write the per-spoke ground-truth files the tmux/ps stubs read.

        Two INDEPENDENT facts (#301): `pane_exists` — tmux still shows a pane (the
        launcher `zsh` outlives the agent) — and `alive` — a `claude` descendant is
        in the process tree. A dead agent is pane_exists=1, alive=0 (the #301 shape);
        a gone pane is pane_exists=0 (the #290 dead-idle shape)."""
        d = self.state_dir / "panes"
        d.mkdir(exist_ok=True)
        (d / f"{spoke.issue}.alive").write_text("1" if spoke.agent_alive else "0")
        (d / f"{spoke.issue}.pane_exists").write_text("1" if spoke.pane_exists else "0")
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
            # A fast plain-text answerer stub: `ANSWER: <text>` is the reasoner's decision
            # contract (parse_decision), and a plain-text stub passes through the
            # stream-json normalizer untouched. Overriding this is REQUIRED — the default
            # command shells the real `claude`, which burns the timeout in CI.
            AFK_ANSWERER_CMD=r"printf 'ANSWER: proceed\n'",
            AFK_AUTH_PROBE_CMD="true",
            AFK_NET_PROBE_CMD="true",
            AI_TOOLKIT_OTEL="0",
            AI_TOOLKIT_GH_LIFECYCLE_LABELS="0",
            AFK_REVIEW_GATE="0",
            HUB_WATCHDOG_FILE="0",
            # Keep the drain's answer/inject/judge paths from spending CI budget on
            # retry/verify sleeps against the stubs (the fake clock only governs reads).
            AFK_INJECT_VERIFY_SECONDS="3",
            AFK_INJECT_POLL_SECONDS="1",
            AFK_INJECT_MENU_PAUSE="0",
            AFK_ANSWERER_TIMEOUT="10",
            AFK_JUDGE_TIMEOUT="5",
            AFK_AUTH_PROBE_TIMEOUT="3",
            AFK_GH_TIMEOUT="3",
            AFK_PLANNER_TIMEOUT="3",
            AFK_NET_PROBE_TIMEOUT="3",
        )
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(k, None)
        # Strip inherited telemetry/OTel env: the harness may run inside a
        # telemetry-on spoke whose OTEL_EXPORTER_OTLP_ENDPOINT points at a shared,
        # busy collector — the drain would inherit it and intermittently block up to
        # ~30s trying to export a span. AI_TOOLKIT_OTEL=0 alone does not cover the
        # raw OTEL_*/CLAUDE_CODE_*TELEMETRY* vars.
        for k in list(env):
            if k.startswith("OTEL_") or ("TELEMETRY" in k) or k.startswith("AI_TOOLKIT_OTEL"):
                env.pop(k, None)
        env["AI_TOOLKIT_OTEL"] = "0"
        env.update(self.env_extra)
        return env

    def run_drain(self, now: int) -> None:
        """Run one real drain tick, wall-clock-capped. A tick's lifecycle records are
        written at the moment each transition happens (the #300 intent-first
        discipline), so a tick that then blocks polling a stub that never changes
        (e.g. waiting for a scripted spoke to `resume`) has already written everything
        the invariants read. The cap keeps CI bounded (the AC) without losing a record;
        it is a harness bound, never asserted on."""
        proc = subprocess.Popen(
            ["bash", str(HUB_AFK), "--once"],
            cwd=str(self.main),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.env(now),
        )
        try:
            proc.wait(timeout=_DRAIN_TICK_CAP_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    # -- transition-log writes (the scripted spoke side) --------------------

    def tlog(
        self,
        issue: int,
        to: str,
        actor: str,
        cause: str,
        *,
        episode: str = "",
        now: int,
    ) -> None:
        """Append a spoke-side transition exactly as the real actor would — authored
        DIRECTLY with a controlled `ts` (the fake clock), since `afk_tlog_transition`
        stamps `ts` from real `date +%s`. The record is byte-identical to the real
        writer's (compact, no-space JSON; the awk/sed readers match `"kind":"..."`).

        A mutation may `drop` a transition name: withholding a recorded state
        reintroduces the pre-#300 "inference over recorded state" world (principle
        1), which is how a fixed watchdog false-fire is reintroduced without editing
        the (out-of-scope) watchdog."""
        if to in self.dropped:
            return
        rec = {
            "v": 1,
            "ts": now,
            "issue": issue,
            "kind": "transition",
            "to": to,
            "actor": actor,
            "cause": cause,
        }
        if episode:
            rec["episode"] = episode
        self._append(issue, rec)

    def event(
        self,
        issue: int,
        event: str,
        actor: str,
        *,
        lane: str = "",
        episode: str = "",
        now: int,
    ) -> None:
        """Append a within-state event (answer_delivered, escalated, nudge, ...)."""
        if event in self.dropped:
            return
        rec = {"v": 1, "ts": now, "issue": issue, "kind": "event", "event": event, "actor": actor}
        if lane:
            rec["lane"] = lane
        if episode:
            rec["episode"] = episode
        self._append(issue, rec)

    def _append(self, issue: int, rec: dict) -> None:
        f = self.state_dir / "transitions" / f"{issue}.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def write_epoch(self, name: str, issue: int, epoch: int) -> None:
        """A bare-integer epoch marker under the state dir (the drain/watchdog seam)."""
        (self.state_dir / f"{name}-{issue}.epoch").write_text(f"{epoch}\n")

    def write_journal(self, issue: int, ts: int, park: str = "gate") -> None:
        """A decision-journal record — the drain's 'I serviced this' evidence."""
        if "journal" in self.dropped:
            return
        line = json.dumps(
            {
                "ts": ts,
                "issue": str(issue),
                "park": park,
                "decision": "answered",
                "reversibility": "reversible",
            },
            separators=(",", ":"),
        )
        with (self.state_dir / "decision-journal.jsonl").open("a") as fh:
            fh.write(line + "\n")

    def git(self, issue: int, *args: str) -> None:
        self._git(*args, cwd=self.spokes[issue].path)

    def set_agent_alive(self, issue: int, alive: bool) -> None:
        self.spokes[issue].agent_alive = alive
        self._sync_pane_state(self.spokes[issue])

    def set_pane_exists(self, issue: int, exists: bool) -> None:
        self.spokes[issue].pane_exists = exists
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


_TMUX_STUB = r"""#!/usr/bin/env bash
# Scripted tmux: reads per-spoke ground truth under {state}/panes/. The window index
# IS the issue, so a pane target round-trips to a stable per-spoke pane pid
# (40000+issue). A pane is listed while its window EXISTS (pane_exists=1) —
# independent of agent liveness, since the launcher zsh outlives a dead claude (#301).
panes_dir="{state}/panes"
case "$1" in
  list-panes)
    for pf in "$panes_dir"/*.path; do
      [ -f "$pf" ] || continue
      iss="$(basename "$pf" .path)"
      [ "$(cat "$panes_dir/$iss.pane_exists" 2>/dev/null)" = "1" ] || continue
      printf 'afk:%s\t%s\n' "$iss" "$(cat "$pf")"
    done
    ;;
  display-message)
    tgt=""
    while [ $# -gt 0 ]; do case "$1" in -t) shift; tgt="$1" ;; esac; shift; done
    printf '%s\n' "$(( 40000 + ${{tgt##*:}} ))"
    ;;
  capture-pane)
    tgt=""
    while [ $# -gt 0 ]; do case "$1" in -t) shift; tgt="$1" ;; esac; shift; done
    iss="${{tgt##*:}}"
    for pf in "$panes_dir"/*.path; do
      [ -f "$pf" ] || continue
      if [ "$(cat "$pf")" = "$tgt" ]; then iss="$(basename "$pf" .path)"; break; fi
    done
    cat "$panes_dir/$iss.pane.txt" 2>/dev/null
    ;;
  send-keys)
    # Model the two-keystroke submit: a literal `-l` paste is remembered, and the
    # following Enter appends it as a type:"user" record to the pane's spoke transcript
    # (what Claude Code writes on submit — the sole proof of delivery, #281), advancing
    # the transcript mtime so the drain's inject-verification registers.
    tgt=""; paste=""; mode=""; enter=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -t) shift; tgt="$1" ;;
        -l) mode="paste" ;;
        --) : ;;
        Enter) enter="1" ;;
        *) [ "$mode" = "paste" ] && paste="$1" ;;
      esac
      shift
    done
    iss="${{tgt##*:}}"
    pbuf="$panes_dir/$iss.paste"
    [ "$mode" = "paste" ] && printf '%s' "$paste" > "$pbuf"
    if [ -n "$enter" ]; then
      wt="$(cat "$panes_dir/$iss.path" 2>/dev/null)"
      [ -n "$wt" ] || exit 0
      munged="$(printf '%s' "$wt" | sed 's/[^A-Za-z0-9]/-/g')"
      jsonl="${{CLAUDE_PROJECTS_DIR}}/$munged/session.jsonl"
      mkdir -p "$(dirname "$jsonl")" 2>/dev/null || true
      if [ -s "$pbuf" ]; then
        _AFK_TXT="$(cat "$pbuf")" python3 -c 'import json,os,sys; sys.stdout.write(json.dumps({{"type":"user","message":{{"content":[{{"type":"text","text":os.environ["_AFK_TXT"]}}]}}}},ensure_ascii=False)+chr(10))' >> "$jsonl" 2>/dev/null
        : > "$pbuf"
      else
        printf '{{}}\n' >> "$jsonl"
      fi
    fi
    ;;
  *) : ;;
esac
exit 0
"""

_PS_STUB = r"""#!/usr/bin/env bash
# #301 probe: answer only the exact -eo form; build a per-spoke table. A `claude`
# child of pane pid 40000+issue exists iff that spoke's agent is alive.
# AFK_SIM_PS_FORCE_ALIVE=1 makes the probe LIE (report a live agent for a dead one)
# — the pre-#301 pane_current_command=zsh proxy, the #301 mutation seam.
case "$*" in
  "-eo pid=,ppid=,comm=")
    for pe in "{state}"/panes/*.pane_exists; do
      [ -f "$pe" ] || continue
      [ "$(cat "$pe")" = "1" ] || continue
      iss="$(basename "$pe" .pane_exists)"
      ppid=$(( 40000 + iss )); apid=$(( 50000 + iss ))
      printf '%s 1 -zsh\n' "$ppid"
      if [ "$(cat "{state}/panes/$iss.alive" 2>/dev/null)" = "1" ] || [ "${{AFK_SIM_PS_FORCE_ALIVE:-0}}" = "1" ]; then
        printf '%s %s claude\n' "$apid" "$ppid"
      fi
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
    now = world.t0 + int(step["t"])  # absolute fake-clock epoch for this step
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
    """A PLAN-gate park: a gate/<issue> tag at tip (slot_state=waiting, answer lane),
    a live agent (pane + ps), a `parked` transition with a minted episode, and the
    pre-stamped park-onset epoch the watchdog's ceiling clock measures from."""
    spoke = world.spokes[issue]
    _seed_transcript(world, issue)
    # A PLAN-gate pane shows a plan awaiting approval — NOT a yes/no permission dialog
    # (which would classify as the broker's permission lane, not the answer lane the
    # park-unanswered detector owns).
    _pane_text(world, issue, "Here is my implementation plan for review.\nAwaiting gate approval.")
    world.git(issue, "tag", "-f", f"gate/{issue}")
    spoke.episode = f"sig{issue}:{now}"
    world.write_epoch("park-onset", issue, now)
    world.tlog(issue, "parked", "reconciler", "parked on the gate", episode=spoke.episode, now=now)


def _v_answer(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    """The drain services the park — SCRIPTED as the drain-authored records a real
    answer_pass writes. `via` selects which recency signal the watchdog reads:
    `journal` (a decision-journal entry, default) or `progress` (a progress-epoch
    advance). Both are paired with an episode-keyed `answer_delivered` event. A
    mutation `drop`s these to reintroduce the pre-#300 unrecorded-service that
    false-fired the watchdog (#263/#265/#283/#288)."""
    via = step.get("via", "journal")
    if via == "progress":
        if "progress" not in world.dropped:
            world.write_epoch("progress", issue, now)
    else:
        world.write_journal(issue, now)
    world.event(
        issue,
        "answer_delivered",
        "hub-inject.sh",
        lane="answer",
        episode=world.spokes[issue].episode,
        now=now,
    )


def _v_push(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    world.tlog(issue, "pushing", "spoke-push.sh", "pushing branch", now=now)
    world.tlog(issue, "pushed", "spoke-push.sh", "pushed; gate green", now=now)


def _v_ready(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    world.git(issue, "tag", "-f", f"ready/{issue}")
    world.tlog(issue, "ready", "spoke-ready.sh", "ready marker at tip", now=now)


def _v_commit(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    world.git(issue, "commit", "-q", "--allow-empty", "-m", step.get("msg", "work"))


def _v_kill_agent(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    """#301: the agent (claude) dies but its launcher zsh keeps the pane alive."""
    world.set_agent_alive(issue, False)


def _v_kill_pane(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    """#290: the whole tmux window is gone (a reboot/crash) — the dead-idle shape."""
    world.set_pane_exists(issue, False)
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


def _v_epoch(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    """Stamp a bare-epoch drain marker at the fake-clock time (e.g. the dispatch
    epoch the watchdog's dead-idle clock ages from)."""
    world.write_epoch(step["name"], issue, now)


def _v_journal(world: World, issue: int, now: int, step: dict, mutation: dict | None) -> None:
    """A bare decision-journal entry with NO real service behind it — a stale record
    that the watchdog reads as recent servicing. Used by a mutation to falsely suppress
    the #310 backstop (the drain looked busy, so nothing escalated the jam)."""
    world.write_journal(issue, now)


_VERBS = {
    "park": _v_park,
    "push": _v_push,
    "ready": _v_ready,
    "commit": _v_commit,
    "kill_agent": _v_kill_agent,
    "kill_pane": _v_kill_pane,
    "land_start": _v_land_start,
    "block": _v_block,
    "answer": _v_answer,
    "epoch": _v_epoch,
    "journal": _v_journal,
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


_FORWARD_STATES = {
    "ready",
    "accepted",
    "pushing",
    "pushed",
    "landing",
    "landed",
    "reaped",
    "revived",
    "redispatched",
}
_INJECT_EVENTS = {"answer_injected", "approval_injected", "nudge"}
_SERVICE_EVENTS = {"answer_delivered", "approval_injected", "waived"}
_ESCALATE = {"escalated", "answer_dropped"}


def inv_unserviced_park_backstopped(world: World, scenario: dict) -> list[Violation]:
    """I3 (#310): the watchdog is the BACKSTOP. A park declared past the ceiling AND
    unserviced MUST be fired by the watchdog (which then intervenes) — never left to
    rot. A stale record that falsely reads as "serviced" suppresses the backstop and
    reproduces the #310 silent 10-hour jam (principle 3: act when unattended; a stall
    jams every dependent)."""
    out: list[Violation] = []
    for issue in _scenario_issues(scenario):
        truth = _spoke_truth(scenario, issue)
        if not (truth.get("parked_past_ceiling") and not truth.get("serviced")):
            continue
        fired = any(
            f.get("condition") in {"park-unanswered", "park-undeliverable"}
            for f in world.fires(issue)
        )
        if not fired:
            out.append(
                Violation(
                    "I3",
                    issue,
                    f"#{issue} parked past the ceiling and was NEVER serviced, yet the "
                    f"watchdog never fired — nothing escalated it (the #310 silent jam: "
                    f"a stall jams every scope-dependent issue behind it, principle 3).",
                )
            )
    return out


def inv_answered_park_advances(world: World, scenario: dict) -> list[Violation]:
    """I4 (#312): once a park episode is serviced (`answer_delivered`), the spoke
    ADVANCES — a forward transition follows — and the same episode is never re-fired
    park-unanswered by the watchdog (principles 5, 6: a serviced episode is settled;
    never re-interpret it as still-unanswered)."""
    out: list[Violation] = []
    for issue in _scenario_issues(scenario):
        records = world.records(issue)
        delivered = [(i, r) for i, r in enumerate(records) if r.get("event") == "answer_delivered"]
        if not delivered:
            continue
        first = delivered[0][0]
        later = [r.get("to") for r in records[first + 1 :] if r.get("kind") == "transition"]
        # The POSITIVE advance is asserted where the scenario declares the spoke should
        # advance after the answer (truth.advances) — never merely "no bad fire" (the
        # #314 guidance). A suppression-only scenario (#263) legitimately ends at the
        # answer and does not opt in.
        if _spoke_truth(scenario, issue).get("advances") and not (set(later) & _FORWARD_STATES):
            out.append(
                Violation(
                    "I4",
                    issue,
                    f"#{issue} was answered (answer_delivered) but recorded no forward "
                    f"transition afterward (later={later}) — an answered park must "
                    f"advance, not re-park (principle 5).",
                )
            )
        # Re-fire half: an episode serviced by the drain must not be re-fired.
        episodes = {r.get("episode") for _, r in delivered if r.get("episode")}
        for fire in world.fires(issue):
            if fire.get("condition") == "park-unanswered" and episodes:
                out.append(
                    Violation(
                        "I4",
                        issue,
                        f"#{issue} was re-fired park-unanswered after a recorded "
                        f"answer_delivered — the serviced episode was re-interpreted "
                        f"as unanswered (principle 6: absence is not evidence).",
                    )
                )
    return out


def inv_serviced_park_never_fired(world: World, scenario: dict) -> list[Violation]:
    """I6 (#263/#265/#283/#288): a park the scenario declares the drain SERVICED is
    never fired park-UNANSWERED ("no answer delivered") by the watchdog. The drain
    records its service (a decision-journal entry / a progress epoch / an episode-keyed
    answer_delivered); the watchdog reads that record and suppresses. Reintroducing the
    pre-#300 unrecorded service (dropping the record) is what false-fired four times
    (principles 1, 6). NB park-UNDELIVERABLE is the HONEST reason for a serviced park
    whose delivery dropped (#288) — not a false silence — so it is not flagged here."""
    out: list[Violation] = []
    for issue in _scenario_issues(scenario):
        if not _spoke_truth(scenario, issue).get("serviced"):
            continue
        bad = [f for f in world.fires(issue) if f.get("condition") == "park-unanswered"]
        if bad:
            out.append(
                Violation(
                    "I6",
                    issue,
                    f"#{issue} park was serviced by the drain but the watchdog fired "
                    f"{[f['condition'] for f in bad]} — a recorded service was read as "
                    f"unanswered (principle 1).",
                )
            )
    return out


def inv_dead_agent_never_injected(world: World, scenario: dict) -> list[Violation]:
    """I5 (#301): a spoke whose scenario declares the agent DEAD never receives an
    inject (text into a dead pane executes as shell) and is instead recovered
    (`revived`/`redispatched`); a live parked agent is serviced, never revived-as-dead
    (principle 4: probe the real process, never a `zsh`/pane proxy)."""
    out: list[Violation] = []
    for issue in _scenario_issues(scenario):
        agent = _spoke_truth(scenario, issue).get("agent")
        events = [e.get("event") for e in world.events(issue)]
        states = {t["to"] for t in world.transitions(issue)}
        if agent == "dead":
            injected = [e for e in events if e in _INJECT_EVENTS]
            if injected:
                out.append(
                    Violation(
                        "I5",
                        issue,
                        f"#{issue} agent was DEAD but received inject events "
                        f"{injected} — injecting into a dead pane runs prose as shell "
                        f"in a worktree (principle 4).",
                    )
                )
            # Positive advance: a dead agent must be RECOVERED, not left (principle 3).
            if _spoke_truth(scenario, issue).get("recovers") and not (
                states & {"revived", "redispatched"}
            ):
                out.append(
                    Violation(
                        "I5",
                        issue,
                        f"#{issue} agent was DEAD but the drain recorded no "
                        f"revived/redispatched recovery (states={sorted(states)}) — a "
                        f"dead agent must be recovered, never left (principle 3).",
                    )
                )
    return out


# The fixed registry: declared ONCE, checked against EVERY scenario. Adding a
# scenario never touches this list; only a change to an AFK Design PRINCIPLE does.
INVARIANTS = [
    inv_pushed_ready_lands,
    inv_landing_never_dead_pane,
    inv_unserviced_park_backstopped,
    inv_answered_park_advances,
    inv_dead_agent_never_injected,
    inv_serviced_park_never_fired,
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
    # A mutation may INJECT extra timeline steps (e.g. a stale false-service record that
    # suppresses the watchdog backstop) — merged in and re-sorted by fake-clock time.
    steps = list(scenario["timeline"])
    if mutation:
        steps += mutation.get("inject", [])
    for step in sorted(steps, key=lambda s: int(s["t"])):
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
