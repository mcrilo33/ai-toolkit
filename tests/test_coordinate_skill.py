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
    "orca orchestration run-use --id", "orca orchestration run-create", "--from $H", "check --run", "--ack", "reply --run", "--id <message-id>",
    '"approve"', '"approve with: ', '"revise: ', "land.sh --review", "dispatch.sh --address", "dispatch.sh --next", "coordinator.sh --status",
    "/coordinate auto", "--answer auto", "--until HH:MM", "--drain", "/coordinate attended", "coordinator.sh --stop --run", "You have", "heartbeat",
    "land.sh --cleanup-only", "worker-release", "blocked", "thread_id", "a land is in flight", "PERMISSION REQUEST", '--body "allow"', '--body "deny"',
    "Permission answered by the user", "bug-scoper", "followup-scoper", "hold", "COORD_IDLE_MIN", "orca orchestration worker-show --dispatch", "observation.agentWait", "orca terminal read"])
def test_the_skill_covers_each_mechanic_of_the_brief(needle):
    assert needle in SKILL


def test_every_script_and_flag_the_skill_names_exists():
    for script, flags in re.findall(r"([a-z]+\.sh)((?: (?:--[a-z-]+|<[a-z-]+>|\d+))*)", SKILL):
        text = (V2 / "scripts" / script).read_text()
        for flag in re.findall(r"--[a-z-]+", flags):
            assert flag in text, f"{script} has no {flag}"


def test_every_orchestration_verb_the_skill_names_exists_in_orca():
    verbs = set(re.findall(r"orca orchestration ([a-z-]+)", SKILL))
    assert {"run-use", "run-create", "check", "reply"} <= verbs and verbs <= ORCHESTRATION_VERBS


def test_a_question_is_turned_into_exactly_one_of_three_replies_and_never_a_blind_approve():
    q = section("On each message")
    assert q.count('--body "approve"') + q.count('"approve with: ') + q.count('"revise: ') >= 3 and "afk-answering" in q and re.search(r"never .*approve|unless the user", q, re.I)


def test_a_permission_question_is_shown_to_the_user_answered_only_with_their_decision_and_commented_without_the_command():
    q = section("On each message")
    p = q[q.index("PERMISSION REQUEST"):]
    p = p[:p.index("**`worker_done` succeeded**")]   # that question type's paragraph
    assert "not a plan gate" in p and "allow" in p and "deny" in p and re.search(r"never .*without the user", p, re.I | re.S)
    assert re.search(r"tool and the (answer|decision) only|never the command", p, re.I) and "stale" in p and "afk-answering" not in p


def test_the_answerer_rule_and_the_planning_rule_say_a_permission_question_is_never_auto_approved():
    afk, hub = (SHARED / "rules/on-demand/afk-answering.md").read_text(), (SHARED / "rules/on-demand/planning-hub.md").read_text()
    assert "PERMISSION REQUEST" in afk and re.search(r"never .*(approv|allow)", afk, re.I) and "deny" in afk
    assert "permission" in hub.lower() and "relay" in hub.lower()


def test_the_switch_never_reads_state_files_for_the_summary():
    summary = section("`/coordinate attended`")
    assert all(w in summary for w in ("gh ", "git log", "orca orchestration inbox", "orca worktree"))
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
    attended = section("`/coordinate attended`")
    assert re.search(r"must not (check|handle)", attended) and re.search(r"exits? 0", attended)


def test_hub_lands_with_the_review():
    assert "land.sh --review" in (SHARED / "skills/hub/SKILL.md").read_text()
