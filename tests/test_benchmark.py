"""Keeps the README's offline benchmark honest: it must run against the real tools and
show the reduction it claims."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))
import compare  # noqa: E402


def test_every_scenario_is_smaller_through_jira_lean():
    rows = compare.scenarios()
    assert len(rows) == 7
    for name, raw, lean in rows:
        assert 0 < lean < raw, name


def test_overall_reduction_is_at_least_eighty_percent():
    rows = compare.scenarios()
    raw, lean = sum(r[1] for r in rows), sum(r[2] for r in rows)
    assert 1 - lean / raw > 0.8


def test_write_responses_carry_identifiers_only():
    rows = {name: lean for name, _, lean in compare.scenarios()}
    assert rows["Add a comment (response only)"] < 120
    assert rows["Create a Confluence page (response only)"] < 120
