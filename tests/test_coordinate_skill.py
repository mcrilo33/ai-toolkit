"""Policy lint of the /coordinate skill (WP7): the session only converses and decides, every mechanic it names must exist."""
import re
from pathlib import Path

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
    "land.sh --review", "blocked", "hold", "PERMISSION REQUEST", "allow", "deny", "COORD_IDLE_MIN", "orca orchestration worker-show --dispatch", "observation.agentWait", "orca terminal read",
    "orca orchestration run-list", "afk-answering", "Scope:", "git log --since", "worker-list",
    "AI_TOOLKIT_DIR", "no toolkit guards", "bin/claude-spoke", "--wait", "run_in_background", "COORD_WATCH_SEC", "7200000"])   # the guards precondition: a session not started through the launcher says so
def test_the_skill_covers_each_mechanic_of_the_brief(needle):
    assert needle in SKILL


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


def test_afk_and_hub_point_at_coordinate_instead_of_duplicating_it():
    afk, hub = (SHARED / "skills/afk/SKILL.md").read_text(), (SHARED / "skills/hub/SKILL.md").read_text()
    assert "/coordinate auto" in afk and "/coordinate" in hub
    assert "terminal create" not in afk and "--reply" not in hub and "--answer" not in hub


def test_the_architecture_doc_describes_both_modes_the_switch_and_the_attended_flow():
    doc = next(p for p in (ROOT / "docs/architecture.md", ROOT / "docs/v2/architecture.md") if p.exists()).read_text()  # before/after cutover
    assert all(w in doc for w in ("/coordinate", "attended", "--stop", "taken back", "```mermaid"))
    assert all(w in doc for w in ("PermissionRequest", "permission-relay.sh", "PERMISSION REQUEST", "fail-closed", "allow"))  # the permission relay and how it fails


def test_between_stop_and_its_exit_the_session_handles_nothing():
    attended = section("Switching")
    assert re.search(r"Never start the other loop", attended) and re.search(r"exits? 0", attended)
    assert "chat session" in attended and "coordinator-stop" in attended   # --stop needs no terminal handle of its own
    assert "run_in_background" in attended and "7200000" in attended   # a foreground Bash call is killed at 600 s, before the exit 0/1 can return


def test_a_chat_session_decision_is_alerted_by_a_mandatory_ask_user_question_card():
    chat = SKILL[SKILL.index("**Chat session.**"):].split("\n")[0]   # no terminal to ring: the card is the alert, so it cannot be optional
    assert "AskUserQuestion" in chat and "optionally" not in chat and "permission request" in chat
    assert "no bell" not in chat.lower() and "afplay" not in chat and "PushNotification" in chat
    assert re.search(r"permission request .{0,40}card is open", chat)   # a request waking the watcher behind an open card


def test_hub_lands_with_the_review():
    assert "land.sh --review" in (SHARED / "skills/hub/SKILL.md").read_text()
