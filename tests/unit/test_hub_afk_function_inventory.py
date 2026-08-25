"""Function-accounting governance for the hub-afk-<lane>.sh split family (issue #351).

Every hub-afk*.sh module is sourced into ONE shared function namespace (bash has no
per-file scoping — the entry lib sources every module into the same shell), so a
copy-paste slip during a split could define the same function in two files at once, or a
rename could leave a stale copy behind in the old file. This is the standing proof that a
split (#307, #308, #351, and any that follow in this family) never loses or duplicates a
function: walk every hub-afk*.sh file's top-level function definitions and assert each
name is defined in EXACTLY one file.

This complements, but does not replace, the per-module mirror tests
(test_hub_afk_<lane>.py): those pin that a SPECIFIC named function reachable through the
entry resolves to a SPECIFIC module file; this test is the repo-wide sweep that would catch
a name accidentally left in two files (or two modules) that the mirror tests don't cover.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HUB_SCRIPTS_DIR = REPO_ROOT / "shared" / "skills" / "hub" / "scripts"

# A top-level bash function definition: `name() {` or `function name {`, optionally
# preceded by whitespace (never inside another block — a nested helper is not part of
# this family's public/registered function surface, and none of the hub-afk*.sh modules
# define nested functions).
_FUNC_DEF_RE = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{", re.MULTILINE)


def _function_defs() -> dict[str, list[str]]:
    """Map function name -> the hub-afk*.sh basenames that define it."""
    defs: dict[str, list[str]] = defaultdict(list)
    for path in sorted(HUB_SCRIPTS_DIR.glob("hub-afk*.sh")):
        for match in _FUNC_DEF_RE.finditer(path.read_text()):
            defs[match.group(1)].append(path.name)
    return defs


def test_no_function_defined_in_more_than_one_hub_afk_module() -> None:
    dupes = {name: files for name, files in _function_defs().items() if len(files) > 1}
    assert dupes == {}, (
        "function(s) defined in more than one hub-afk*.sh module (a split must MOVE code, "
        f"never copy it): {dupes}"
    )


def test_every_hub_afk_module_defines_at_least_one_function() -> None:
    # A module with zero functions is either dead weight or a failed extraction that left
    # everything behind in the entry — catch either before it ships.
    counts: dict[str, int] = defaultdict(int)
    for _name, files in _function_defs().items():
        for f in files:
            counts[f] += 1
    for path in HUB_SCRIPTS_DIR.glob("hub-afk*.sh"):
        assert counts.get(path.name, 0) > 0, f"{path.name} defines no functions"
