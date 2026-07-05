"""Guard against orphaned test fixtures in ``tests/unit/fixtures/`` (Issue #145).

When a test's ``.py`` consumer is removed but its checked-in fixture is left behind,
the fixture becomes tracked dead weight — exactly what happened to six
``dashboard_*.jsonl`` files when the Streamlit dashboard was retired (#90) and only
surfaced during the #140 sweep. This test fails if any fixture in
``tests/unit/fixtures/`` is not referenced (by basename) from at least one tracked
source file, so a future orphan is caught at land time rather than years later.

The reference scan covers Python sources under ``tests/`` and shell/Python under
``scripts/`` — the two places a fixture is legitimately loaded from — matching either
the full filename (``foo.jsonl``) or its stem (``foo``).
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES_DIR = _REPO_ROOT / "tests" / "unit" / "fixtures"

# Directories whose sources may legitimately reference a fixture.
_SEARCH_ROOTS = (_REPO_ROOT / "tests", _REPO_ROOT / "scripts")
_SEARCH_SUFFIXES = (".py", ".sh")


def _source_texts() -> list[str]:
    texts: list[str] = []
    for root in _SEARCH_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix in _SEARCH_SUFFIXES and path.is_file():
                texts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return texts


def test_no_orphaned_fixtures() -> None:
    if not _FIXTURES_DIR.is_dir():
        return  # No fixtures directory → nothing can be orphaned.

    fixtures = sorted(p for p in _FIXTURES_DIR.iterdir() if p.is_file())
    sources = _source_texts()

    orphans = [
        fixture.name
        for fixture in fixtures
        if not any(fixture.name in text or fixture.stem in text for text in sources)
    ]

    assert not orphans, "Orphaned test fixtures (no tracked source references them): " + ", ".join(
        orphans
    )
