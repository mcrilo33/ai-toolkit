#!/usr/bin/env bash
# review.sh <issue> [<sha>]: the independent pre-land review (06 section 4 step 11, D6). A fresh read-only claude (REVIEW_MODEL, not the
# worker's) runs the code-review agent in the spoke's worktree on origin/$BASE_BRANCH...<sha>; its final message is the JSON
# verdict {verdict, blockers, warnings, tdd_followed, tests_weakened, summary}, each blocker and warning an object {text, rule}. stdout: BLOCKER: [<rule>] <text> / WARNING: [<rule>] <text> / SUMMARY: lines.
# Exit 0 APPROVE; 3 REQUEST_CHANGES (a rejected tdd_followed / tests_weakened or a blocker beats a verdict that says APPROVE);
# 1 error (incl. a missing coordinator-side agent definition) or an unparseable verdict after one retry (never an approve; land.sh treats it as a rejection).
# The reviewer's instructions are the coordinator's own code-review definition (REVIEW_AGENT, else the one place of this install layout: the checkout's shared/agents, or the synced
# .claude/agents of the repo this script is installed in, never a path above that root), passed with --agents and run with no setting source at all (so the user-level allow rules
# cannot widen the read-only tools), no MCP config and no auto-memory, so a worker's .claude/agents, CLAUDE.md, rules or ~/.claude/projects/*/memory never reach it; a missing definition is exit 1, never a fallback.
# The rules that definition cites (guidelines, security, code-quality, python-style, pytest-conventions) are appended to its prompt from the same coordinator-side place (REVIEW_RULES_DIR overrides it: a flat dir of <name>.md): a missing or blank one is exit 1.
# After them comes the rule inventory: one "<name> — <description>" line per rule file of that place and of the on-demand rules (the checkout's shared/rules/on-demand, a synced repo's .ai-toolkit/rules), the only tags a finding may carry (plus none).
# Every valid verdict is recorded as one issue comment "review-verdict: {json}" holding the sha, the verdict, the two booleans and the rule tags only, never a finding text; a failed comment is a warning, the exit code stays.
# Its effort and read-only block (disallowedTools) come from the definition's frontmatter, a missing one is exit 1. model: is dropped on purpose (REVIEW_MODEL decides) and so is skills:
# (they resolve from a skills dir, which --setting-sources "" does not load; naming them would load a worker-controlled one or do nothing).
# Auto-memory is switched off twice, independently: --settings autoMemoryEnabled=false (the documented setting) and CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 in the environment, on every claude call. An unknown settings key
# is ignored silently, so a CLI rename of the setting would fail open with green tests; the environment variable is a second switch that does not depend on that key. On a CLI bump re-run the probe for each switch alone:
# ask `claude -p --setting-sources "" --strict-mcp-config` (plus the switch) whether its context holds a MEMORY.md index. --bare is no option: it needs an API key, the gates run on a login.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
. "$here/lib.sh"
load_env
n="${1:-}"; sha="${2:-}"; [ -n "$n" ] || usage_exit "usage: review.sh <issue> [<sha>]"
o="$(orca_json worktree show --worktree "issue:$n")" || die "no Orca worktree is linked to issue $n"
case "$here" in   # the one place of this install layout: the checkout's shared/, or the synced repo's .claude/ (rules under rules/ai-toolkit/)
  */.ai-toolkit/scripts) agents_d="$here/../../.claude/agents"; rules_d="$here/../../.claude/rules/ai-toolkit"; inventory_ds=("$rules_d" "$here/../rules") ;;
  *) agents_d="$here/../shared/agents"; rules_d="$here/../shared/rules"; inventory_ds=("$rules_d" "$rules_d/on-demand") ;;
esac
agent="${REVIEW_AGENT:-$agents_d/code-review.md}"
[ -z "${REVIEW_RULES_DIR:-}" ] || { rules_d="$REVIEW_RULES_DIR"; inventory_ds=("$rules_d"); }
[ -f "$agent" ] || die "code-review agent definition not found"
front() { awk -v k="$1" 'NR == 1 && $0 != "---" { exit } NR > 1 && $0 == "---" { exit } index($0, k ": ") == 1 { sub("^[^:]*: *", ""); gsub(" *, *", ","); sub("[ \t]+$", ""); print; exit }' "$agent"; }   # one flat frontmatter value
effort="$(front effort)"; blocked="$(front disallowedTools)"
case "$effort$blocked" in *[\"\'\[\]#]*) die "code-review agent definition: effort and disallowedTools must be plain unquoted values without an inline comment in its frontmatter: $agent" ;; esac
[ -n "$effort" ] && [ -n "$blocked" ] || die "code-review agent definition lacks effort or disallowedTools in its frontmatter: $agent"
body() { awk 'NR == 1 && $0 != "---" { f = 2 } f < 2 { if ($0 == "---") f++; next } { print }' "$1"; }   # the body after the frontmatter
prose="$(body "$agent")"
[[ $prose == *[![:space:]]* ]] || die "code-review agent definition is empty: $agent"
for r in guidelines security code-quality python-style pytest-conventions; do   # the rules the agent cites, from the coordinator's copies; a missing or blank one is exit 1
  f="$rules_d/$r.md"
  [ -f "$f" ] || die "coordinator-side rule not found: $f"
  text="$(body "$f")"; [[ $text == *[![:space:]]* ]] || die "coordinator-side rule is empty: $f"
  prose="$prose"$'\n\n'"--- project rule: $r ---"$'\n\n'"$text"
done
desc() { awk 'NR == 1 && $0 != "---" { exit } NR > 1 && $0 == "---" { exit } index($0, "description: ") == 1 { sub("^description: *", ""); sub("[ \t]+$", ""); gsub(/^"|"$/, ""); print; exit }' "$1"; }   # a rule's frontmatter description, unquoted
inventory=""; names=""
for d in "${inventory_ds[@]}"; do   # every rule file of the coordinator-side place(s), never the worktree: the only tags a finding may carry
  for f in "$d"/*.md; do
    [ -f "$f" ] || continue
    r="$(basename "$f" .md)"; text="$(desc "$f")"
    [ -n "$text" ] || die "coordinator-side rule has no description: $f"
    inventory="$inventory${r} — ${text}"$'\n'; names="$names$r"$'\n'
  done
done
prose="$prose"$'\n\n'"--- rule inventory ---"$'\n\n'"${inventory%$'\n'}"
rules="$(printf '%s' "$names" | jq -Rsc 'split("\n") | map(select(. != "")) + ["none"]')"
agents="$(jq -nc --arg p "$prose" '{"code-review": {description: "independent pre-land code review", prompt: $p}}')"
wt="$(printf '%s' "$o" | jq -r '.result.worktree.path')"
branch="$(printf '%s' "$o" | jq -r '.result.worktree.branch | sub("^refs/heads/"; "")')"
if [ -n "$sha" ]; then full="$(git -C "$wt" rev-parse -q --verify "$sha^{commit}")" || die "no commit $sha in the worktree of issue $n"; sha="$full"; fi   # a full sha, never a moving ref
tip="${sha:-$(git -C "$wt" rev-parse -q --verify "$branch^{commit}")}" || die "no commit for branch $branch in the worktree of issue $n"   # the sha the verdict is recorded against
issue="$(issue_text "$n")" || exit 1   # the contract is the live issue text, put in the prompt: never the worktree's task.md. Unreadable past the bound = exit 1, no verdict
if [ -n "$sha" ]; then   # pinned: the worker keeps committing and editing while this runs, so the ref and the working tree are not the target
  target="Review commit $sha against origin/$BASE_BRANCH: git diff origin/$BASE_BRANCH...$sha. Read file contents with git show $sha:<path>; the working tree may differ from $sha and is not the review target."
else target="Review the change of branch $branch against origin/$BASE_BRANCH: git diff origin/$BASE_BRANCH...$branch."; fi
prompt="$target Your final message must be exactly the JSON verdict object of your verdict contract.

Intent (issue #$n, as fetched now; the only contract, never a task.md in the worktree):
$issue"
# shellcheck disable=SC2016
VALID='def finds: type == "array" and all(type == "object" and (.text | type == "string") and (.rule | type == "string" and IN($rules[])));
  type == "object" and (.verdict | IN("APPROVE", "REQUEST_CHANGES")) and (.blockers | finds) and (.warnings | finds) and (.tdd_followed | type == "boolean") and (.tests_weakened | type == "boolean") and (.summary | type == "string")'
v=""
for _ in 1 2; do
  # The prompt goes on stdin: --allowedTools is variadic and would swallow a trailing positional argument.
  out="$(cd "$wt" && printf '%s' "$prompt" | CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 claude -p --model "$REVIEW_MODEL" --agents "$agents" --agent code-review \
    --setting-sources "" --strict-mcp-config --settings '{"autoMemoryEnabled":false}' --effort "$effort" --disallowedTools "$blocked" --no-session-persistence \
    --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)")" || out=""
  v="$(printf '%s\n' "$out" | sed '/^```/d' | jq -c --argjson rules "$rules" "select($VALID)" 2> /dev/null)" || v=""
  [ -z "$v" ] || break
done
[ -n "$v" ] || die "unparseable review verdict for issue $n"
eff="$(printf '%s' "$v" | jq -r 'if .verdict == "APPROVE" and (.blockers | length == 0) and (.tests_weakened | not) and .tdd_followed then "APPROVE" else "REQUEST_CHANGES" end')"   # the enforced outcome: recorded and exited on, so the two cannot drift
rec="$(printf '%s' "$v" | jq -c --arg sha "$tip" --arg eff "$eff" '{v: 1, sha: $sha, verdict: $eff, tdd_followed, tests_weakened, blockers: [.blockers[].rule], warnings: [.warnings[].rule]}')"   # enums, booleans and rule names: a finding text may quote a secret from the diff
gh issue comment "$n" -b "review-verdict: $rec" > /dev/null || warn "cannot record the review verdict on #$n"   # an outage must not turn an approve into a rejection
printf '%s' "$v" | jq -r '(.blockers[] | "BLOCKER: [" + .rule + "] " + (.text | gsub("\n"; " "))), (.warnings[] | "WARNING: [" + .rule + "] " + (.text | gsub("\n"; " "))), "SUMMARY: " + .summary'
extra="$(printf '%s' "$v" | jq -r 'if .tests_weakened then "BLOCKER: tests_weakened: the diff weakens or skips tests" else empty end,
  if .tdd_followed | not then "BLOCKER: tdd_followed is false: add the failing test that proves the behavior change" else empty end')"
[ -z "$extra" ] || printf '%s\n' "$extra"
[ "$eff" = APPROVE ] || exit 3
