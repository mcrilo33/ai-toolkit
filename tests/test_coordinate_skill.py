"""Policy lint of the /coordinate skill (WP7): the session only converses and decides, every mechanic it names must exist."""
import json
import re

import pytest
from conftest import ROOT, V2

SHARED = ROOT / "shared"
SKILL = (SHARED / "skills/coordinate/SKILL.md").read_text() if (SHARED / "skills/coordinate/SKILL.md").exists() else ""
ORCHESTRATION_VERBS = {"run-create", "run-use", "run-current", "run-list", "run-show", "send", "check", "reply", "inbox", "task-create", "task-list",
                       "task-update", "worker-start", "worker-show", "worker-read", "worker-stop", "worker-abandon", "worker-release", "worker-retain",
                       "worker-list", "dispatch", "request-show", "dispatch-show", "ask", "gate-create", "gate-resolve", "gate-list", "reset"}


def section(title):
    m = re.search(rf"^#+ {re.escape(title)}.*?\n(.*?)(?=^#+ |\Z)", SKILL, re.S | re.M)
    assert m, f"no section '{title}'"
    return m.group(1)


def test_the_skill_exists_within_its_size_cap_with_claude_frontmatter():
    assert SKILL and len(SKILL.splitlines()) <= 130
    head = SKILL.split("\n---\n")[0]
    assert head.startswith("---\nname: coordinate\n") and "\ndescription: \"" in head and "argument-hint:" in head


@pytest.mark.parametrize("needle", [
    "orca terminal create", "--answer attended", "--session", "$ORCA_TERMINAL_HANDLE", "--show", "ps -o tty= -p $PPID", "printf '\\a'", "PushNotification", "coordinator.sh --status", "--reply <message-id> approve", "'approve with: ", "'revise: ", "--dispatch <n>",
    "one decision at a time", "never binds the Run", "Never show a heartbeat", "/coordinate auto", "--answer auto", "--until HH:MM", "--drain", "/coordinate attended", "coordinator.sh --stop --run",
    "land.sh --review", "blocked", "hold", "PERMISSION REQUEST", "allow", "deny", "COORD_IDLE_MIN", "orca orchestration worker-show --dispatch <id> --full", "observation.agentWait", "orca terminal read",
    "orca orchestration run-list", "afk-answering", "Scope:", "git log --since", "worker-list",
    "AI_TOOLKIT_DIR", "no toolkit guards", "bin/claude-spoke", "--wait", "run_in_background", "COORD_WATCH_SEC", "7200000",
    "hand over now (Handover)", "COORD_HANDOVER_TOKENS", "orca agent launch", "--model claude-opus-5-5", "/coordinate attended --run <run>",
    "/coordinate away", "--answer away", "NOTIFY_WEBHOOK_URL", "--position", "never both", "handed over to the webhook", "HANDOVER_PLAN_S", "attended --until"])   # the guards precondition: a session not started through the launcher says so
def test_the_skill_covers_each_mechanic_of_the_brief(needle):
    assert needle in SKILL


def test_the_handover_is_triggered_by_the_scripts_line_at_an_idle_point_and_never_starts_a_second_loop():
    h = section("Handover")
    assert "200k" in h and re.search(r"state lives in Orca, git and the issue labels, not in the chat", h)   # the threshold and why a fresh session loses nothing
    assert re.search(r"--wait.{0,40}--status.{0,80}hand over now \(Handover\)", h) and "COORD_HANDOVER_TOKENS" in h   # #574: tool output the session reads at every wake
    assert "hand over now (Handover)" in (V2 / "scripts/coordinator.sh").read_text()   # the wording the skill waits for is the one the script prints
    assert re.search(r"never measure.{0,80}no line.{0,20}no handover", h) and "```bash" not in h and ".jsonl" not in h   # no step to remember: unknown prints nothing, no guessing
    assert re.search(r"idle point.{0,120}no decision on screen.{0,80}no `--stop`", h, re.S)   # never mid-decision or mid-switch
    assert re.search(r"never a second loop", h) and "held by: coordinator.sh (attended" in h and "--session" in h   # attach to the running loop; only a terminal successor re-points it
    assert re.search(r"--surface .{0,200}ORCA_TERMINAL_HANDLE", h, re.S)   # a terminal session hands to a terminal: the loop types only into one
    assert re.search(r"launch succeeded.{0,80}TaskStop.{0,60}(watcher|--wait)", h, re.S) and "--retry-launch" in h   # the old watcher would present every decision twice, but only once the successor exists
    assert re.search(r"finished.{0,80}never re-arms `--wait` and never presents again", h, re.S)   # the old chat can still get a turn (a typed loop line, a notification)
    assert re.search(r"presents `--status` while it runs", h)   # no silent window while a terminal successor waits out a land


def test_a_session_not_on_opus_restarts_once_before_its_first_loop_and_an_unknown_model_proceeds():
    p = section("Preconditions")
    assert p.index("AI_TOOLKIT_DIR") < p.index("claude-opus-*")   # after the guards check
    assert re.search(r"claude-opus-\*.{0,60}about to start or attach to its first loop", p, re.S)
    assert re.search(r"never for `status` or a plain question, never once it attends one", p)   # a read opens no chat; an attending session keeps its loop and watcher
    assert re.search(r"\*\*Handover\*\*.s launch line", p) and "--retry-launch" in p
    assert re.search(r'--prompt "/coordinate <[^>]+> --restarted"', p)   # the user's own arguments
    assert re.search(r"restarted on Opus.{0,40}stop.{0,40}this session starts no loop", p)   # never a second loop beside the successor's
    assert re.search(r"unknown.{0,20}proceed.{0,20}never guess", p, re.S)
    assert re.search(r"`--restarted`.{0,40}skips this check", p)   # one restart at most: a successor still off Opus never opens another chat
    assert SKILL.count("claude-opus-5-5") == 1 and "--model claude-opus-5-5" in section("Handover")   # a model bump is a one-line edit


@pytest.mark.parametrize(("config", "turns", "session", "said"), [
    pytest.param("", [("claude-opus-5-5", False), ("claude-fable-5-2", False)], "sid", ["claude-fable-5-2"], id="the-last-turn-counts"),
    pytest.param("acct", [("claude-opus-5-5", False), ("claude-haiku-5-5", True), ("<synthetic>", False)], "sid", ["claude-opus-5-5"], id="only-real-main-thread-turns"),
    pytest.param("", [("claude-fable-5-2", False)], "other", [], id="no-transcript"),
])
def test_the_model_read_prints_this_sessions_main_thread_model_and_unknown_prints_nothing(run, tmp_path, config, turns, session, said):
    d = tmp_path / (config or "home/.claude") / "projects/p"
    d.mkdir(parents=True)
    rows = [json.dumps({"type": "assistant", "isSidechain": side, "message": {"model": model}}) for model, side in turns]
    (d / "sid.jsonl").write_text("\n".join([*rows, '{"type": "user"}', '{"type": "assist']))   # the last line is still being written
    block = re.search(r"```bash\n(.*?)```", section("Preconditions"), re.S)
    assert block, "Preconditions has no model read"
    r = run(["bash", "-c", block[1]], HOME=tmp_path / "home", CLAUDE_CONFIG_DIR=config and tmp_path / config, CLAUDE_CODE_SESSION_ID=session)
    assert r.returncode == 0 and r.stdout.splitlines() == said


def test_every_script_and_flag_the_skill_names_exists():
    for script, flags in re.findall(r"([a-z]+\.sh)((?: (?:--[a-z-]+|<[a-z-]+>|\d+))*)", SKILL):
        text = (V2 / "scripts" / script).read_text()
        for flag in re.findall(r"--[a-z-]+", flags):
            assert flag in text, f"{script} has no {flag}"


def test_every_orchestration_verb_the_skill_names_exists_in_orca():
    verbs = set(re.findall(r"orca orchestration ([a-z-]+)", SKILL))
    assert {"run-list", "worker-list", "worker-show"} <= verbs and verbs <= ORCHESTRATION_VERBS   # the session never binds the Run: no run-use, check or reply of its own


def test_a_question_is_turned_into_exactly_one_of_three_replies_and_never_a_blind_approve():
    q = section("The queue")
    assert q.count("--reply <message-id> ") >= 3 and "afk-answering" in q and re.search(r"never .*approve|unless the user", q, re.I)
    assert re.search(r"being answered by the loop.{0,80}not (a decision|presented)", q, re.I | re.S)   # --status prints a plan the loop still answers apart from the queue


def test_a_permission_question_is_shown_to_the_user_answered_only_with_their_decision_and_commented_without_the_command():
    q = section("The queue")
    p = q[q.index("PERMISSION REQUEST"):]
    p = p[:p.index("- **A blocked issue**")]   # that question type's paragraph
    assert "not a plan gate" in p and "allow" in p and "deny" in p and re.search(r"only on the user's decision", p, re.I | re.S)
    assert re.search(r"tool and the (answer|decision) only|never the command", p, re.I) and "stale" in p and "afk-answering" not in p


def test_the_answerer_rule_and_the_planning_rule_say_a_permission_question_is_never_auto_approved():
    afk, hub = (SHARED / "rules/on-demand/afk-answering.md").read_text(), (SHARED / "rules/on-demand/planning-hub.md").read_text()
    assert "PERMISSION REQUEST" in afk and re.search(r"never .*(approv|allow)", afk, re.I) and "deny" in afk
    assert "permission" in hub.lower() and "relay" in hub.lower()


def test_the_switch_never_reads_state_files_for_the_summary():
    summary = section("Requests")
    assert all(w in summary for w in ("gh ", "git log", "orca orchestration worker-list"))
    assert not re.search(r"AITK_STATE_DIR|holder\.|/replies|\.ai-toolkit/coordinator|state file", summary)


def test_hub_points_at_coordinate_instead_of_duplicating_it():
    hub = (SHARED / "skills/hub/SKILL.md").read_text()
    assert "/coordinate" in hub and "--reply" not in hub and "--answer" not in hub


def test_the_architecture_doc_describes_both_modes_the_switch_and_the_attended_flow():
    doc = next(p for p in (ROOT / "docs/architecture.md", ROOT / "docs/v2/architecture.md") if p.exists()).read_text()  # before/after cutover
    assert all(w in doc for w in ("/coordinate", "attended", "--stop", "taken back", "```mermaid"))
    assert all(w in doc for w in ("Three positions", "away", "NOTIFY_WEBHOOK_URL", "--position", "at-least-once", "ids only", "handed_over", "HANDOVER_PERMISSION_S", "attended --until"))
    assert all(w in doc for w in ("PermissionRequest", "permission-relay.sh", "PERMISSION REQUEST", "fail-closed", "allow"))  # the permission relay and how it fails


def test_between_stop_and_its_exit_the_session_handles_nothing():
    attended = section("Switching")
    assert re.search(r"Never start the other loop", attended) and re.search(r"exits? 0", attended)
    assert "chat session" in attended and "coordinator-stop" in attended   # --stop needs no terminal handle of its own
    assert "run_in_background" in attended and "7200000" in attended   # a foreground Bash call is killed at 600 s, before the exit 0/1 can return


def test_a_chat_session_start_report_names_the_orca_notification_setting_the_card_sound_depends_on():
    start = section("Start")
    assert re.search(r"chat session.{0,200}start report", start) and re.search(r"Settings . Notifications . .Agent Task Complete", start)   # said once, before the first decision, not after a missed one
    assert "notifications enabled" in start and "workspace not muted" in start   # the whole condition under which Orca sounds the card


def test_a_chat_session_decision_is_alerted_by_a_mandatory_ask_user_question_card():
    chat = SKILL[SKILL.index("**Chat session.**"):].split("\n")[0]   # no terminal to ring: the card is the alert, so it cannot be optional
    assert "AskUserQuestion" in chat and "optionally" not in chat and "permission request" in chat
    assert "no bell" not in chat.lower() and "PushNotification" in chat
    assert "Orca raises its sound" not in chat and "no sound command" not in chat   # Orca's card sound is its Agent Task Complete notification, often off: the session cannot rely on it
    assert "Agent Task Complete" in chat and "`afplay /System/Library/Sounds/Glass.aiff`" in chat   # so the session sounds every decision card itself
    assert re.search(r"for a permission request.{0,200}turn 1.{0,60}afplay", chat) and "every decision" not in chat   # only the one decision that expires gets its own sound, in the context turn, before the card
    assert re.search(r"no other decision.{0,40}second sound", chat)   # with Agent Task Complete on, a plan, blocked issue or refused request is not sounded twice
    assert re.search(r"permission request .{0,40}card is open", chat)   # a request waking the watcher behind an open card
    assert "**quick decision**" in chat and "**technical detail**" in chat   # two levels in the chat before the card: decide fast, or deep-dive
    assert "holds only the question" in chat and "#<n>: <choice>?" in chat and "no `preview`" in chat   # a short card question; Orca renders no option preview
    assert re.search(r"option.s description says what", chat)
    assert "inline chat visuals" in chat and "one-line choice" in chat   # a condensed visual in the technical level, none when a line is enough
    assert "folds" in chat and "background `sleep" in chat and "final text, no tool call after" in chat and re.search(r"PushNotification.{0,200}final text", chat)   # Orca folds text written before a tool call: tools first, the context is the turn's last output
    assert re.search(r"card alone.{0,40}no text before", chat) and "turn 1 short" in chat   # turn 2 is the bare card; a short turn 1 keeps a permission request inside its 9 minutes


def test_a_deny_refuses_one_command_and_the_hold_label_is_the_do_not_land_yet_lever_with_its_way_back():
    perm = next(ln for ln in SKILL.split("\n- ") if ln.startswith("**`PERMISSION REQUEST`**"))
    assert "refuses one command, not the land" in perm and "gh issue edit <n> --add-label hold" in perm and re.search(r"hold.{0,40}before (the )?repl", perm)
    land = next(ln for ln in SKILL.split("\n- ") if ln.startswith("**Land this**"))
    assert "`hold`" in land and "do not land yet" in land and "remove `hold`" in land and "--dispatch <n> 'address: send worker_done again'" in land


def test_hub_lands_with_the_review():
    assert "land.sh --review" in (SHARED / "skills/hub/SKILL.md").read_text()


def test_an_issue_awaiting_main_is_not_landed_yet_and_a_rejected_pr_arrives_as_a_blocked_item():
    land = next(ln for ln in SKILL.split("\n- ") if ln.startswith("**Land this**"))
    done = next(ln for ln in SKILL.split("\n- ") if ln.startswith("**What landed**"))
    assert "`status:awaiting-main`" in land and "not landed yet" in land and "merged into `MAIN_BRANCH`" in land and "blocked item" in land
    assert "awaiting-main" in done and "not landed" in done


def test_a_held_finished_issue_is_read_with_gh_not_status_and_only_a_terminal_session_hears_of_it():
    bell = SKILL[SKILL.index("**The bell, the one rule**"):SKILL.index("**Chat session.**")]
    chat = SKILL[SKILL.index("**Chat session.**"):].split("\n")[0]   # coordinator.sh sends the held line only through --session; --wait and --status never list a held issue
    assert "gh issue view <n>" in bell and "Finished but held" in bell and "--status` does not list" in bell
    assert not re.search(r"held issue: `--status`", bell)
    assert "finished issue held: land now" not in chat and "gh issue list --label hold --state open" in chat and "Finished but held" in chat
    assert "an approval: start it (remove `hold`), keep held or close, with its `Hold:` reason" in chat and "also labelled `blocked` is parked on purpose" in chat   # a held issue is an approval with the filer's reason, a parked one is no decision
    assert "never a start/later decision" in chat and "held follow-ups" not in chat
    assert "then each open `hold` issue" in SKILL and "never on an issue the human asked for" in (SHARED / "skills/start-task/SKILL.md").read_text()   # a terminal session also presents them; the hub never holds an issue the human asked for


def test_a_finished_land_human_round_is_presented_as_two_levels_with_a_three_option_card_that_warns_about_the_land():
    chat = SKILL[SKILL.index("**Chat session.**"):].split("\n")[0]
    assert "finished `Land: human` round" in chat and "next round with guidance / land / keep for later" in chat   # --wait wakes for it; the card's three options
    rnd = next(ln for ln in SKILL.split("\n- ") if ln.startswith("**A finished round**"))
    assert "round report" in rnd and "artefact paths" in rnd and "quick decision" in rnd and "technical detail" in rnd
    assert "--dispatch <n> '<guidance>'" in rnd and "Land: auto" in rnd and "--dispatch <n> 'address: send worker_done again'" in rnd
    assert re.search(r"removes the worktree and its gitignored artefacts", rnd) and "gh issue view <n> --comments" in rnd
    assert "never lands a `Land: human` issue" in next(ln for ln in SKILL.split("\n- ") if ln.startswith("**Land this**"))


@pytest.mark.parametrize("path, needles", [
    ("skills/start-task/SKILL.md", ["`Land:`", "`human`", "eval", "prompt", "annotator", "refused at dispatch", "priced estimate", "never a round guess"]),
    ("skills/solo-cycle/SKILL.md", ["`Land: human`", "round report", "absolute paths", "guidance for the next round", "Deferred"]),
    ("rules/on-demand/issue-hygiene.md", ["`Land:`", "`human`", "round report", "`auto`"]),
    ("skills/solo-cycle/SKILL.md", ["only the human can do", "never escalate", "restart the flow", "fresh URL or device code"]),
    ("rules/operational-gotchas.md", ["aws sso login", "aws sts get-caller-identity", "through its `ask`", "In a chat session, if the token", "a spoke restarts the flow"]),
    ("skills/coordinate/SKILL.md", ["never tells the worker to \"escalate\""]),
    ("skills/solo-cycle/SKILL.md", ["grep -m1 '^SPEND_APPROVE_USD='", "never `Read` or `cat`"]),
    ("agents/bug-scoper.md", ["grep -m1 '^UPSTREAM_REPO='", "grep -m1 '^DISPATCH_LABEL='", "never `Read` or `cat`"]),
    ("agents/followup-scoper.md", ["grep -m1 '^UPSTREAM_REPO='", "grep -m1 '^DISPATCH_LABEL='", "never `Read` or `cat`"]),
    ("rules/on-demand/afk-answering.md", ["the human is away", "worker_done --outcome failed"]),
])
def test_the_land_human_footer_is_taught_where_issues_are_filed_worked_and_documented(path, needles):
    text = (SHARED / path).read_text()
    assert all(n in text for n in needles), [n for n in needles if n not in text]


@pytest.mark.parametrize("path", ["skills/solo-cycle/SKILL.md", "agents/bug-scoper.md", "agents/followup-scoper.md", "rules/on-demand/bug-triage.md"])
def test_no_worker_text_tells_the_reader_to_open_the_whole_local_env_file(path):
    text = (SHARED / path).read_text()
    mentions = [m.start() for m in re.finditer(r"ai-toolkit\.local\.env", text)]
    assert mentions and all("grep -m1" in text[max(0, i - 250):i + 250] for i in mentions)
