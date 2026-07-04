"""Deliberately failing test — scratch-branch verification for issue #129.

Exists only on scratch/129-red-verify to make the CI Pytest job go red and
prove the report-red job files exactly one auto-issue (and dedups on the
second red run). Never lands on main.
"""


def test_deliberate_red_for_129_verification() -> None:
    assert False, "deliberate red: exercising report-red issue automation (#129)"


# second red run marker for dedup verification (#129)
