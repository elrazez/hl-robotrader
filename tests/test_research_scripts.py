"""One smoke test per research script: does it run end to end and write a report we can read?

These prove the eight scripts do not rot. They prove nothing about the science: the lake
they run against is four hours of synthetic tape, and a question whose bar asks for eight
calendar months should and does answer `inconclusive` on it. The value is that a schema
change, a renamed feature or a broken join fails here in a second instead of three hours
into the owner's real run.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts" / "research"
LAKE_START = "2026-03-01"
LAKE_END = "2026-03-01"

# Discovered rather than listed, so a tenth question cannot be added without a smoke test.
# The glob takes two digits: q10 is a question, not a typo, and the one-digit form silently
# skipped it.
QUESTIONS = {path.stem: f"{path.stem}.md" for path in sorted(SCRIPTS.glob("q[0-9]*_*.py"))}


def run(name: str, tmp_path: Path, lake: Path) -> None:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(f"research_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    argv = [
        name,
        "--start",
        LAKE_START,
        "--end",
        LAKE_END,
        "--data-dir",
        str(lake),
        "--out-dir",
        str(tmp_path),
    ]
    old, sys.argv = sys.argv, argv
    try:
        module.main()
    finally:
        sys.argv = old


def test_every_question_script_exists() -> None:
    """Phase 3's deliverable was eight questions; Q9 and Q10 are the Phase 7 additions."""
    assert len(QUESTIONS) == 9, f"expected nine question scripts, found {sorted(QUESTIONS)}"


@pytest.mark.parametrize("name,report", sorted(QUESTIONS.items()))
def test_the_script_runs_on_the_fixture_lake_and_writes_a_report_with_a_decision(
    name: str, report: str, tmp_path: Path, research_lake: Path
) -> None:
    run(name, tmp_path, research_lake)
    text = (tmp_path / report).read_text()
    verdict = re.search(r"^## DECISION: (yes|no|inconclusive)$", text, re.MULTILINE)
    assert verdict, f"{report} has no parseable DECISION line"
    # Q5 measures timing, not money, and says so; every other report has to state the cost
    # basis its DECISION is read from.
    if name != "q5_rhythm":
        assert "net-stress" in text, f"{report} never states the cost basis its DECISION reads"


def test_q10_answers_inconclusive_on_every_one_of_its_decisions(
    tmp_path: Path, research_lake: Path
) -> None:
    """Q10 writes three DECISION lines rather than one, and four synthetic hours cannot
    clear a 40-episode floor on any of them. A `yes` here would mean the bar is not wired
    up, not that a made up tape discovered something."""
    run("q10_ladder", tmp_path, research_lake)
    text = (tmp_path / "q10_ladder.md").read_text()
    verdicts = re.findall(r"^## DECISION: (yes|no|inconclusive)$", text, re.MULTILINE)
    assert verdicts == ["inconclusive", "inconclusive", "inconclusive"], text


def test_q10_finds_the_first_bar_that_reaches_a_level_and_nothing_earlier() -> None:
    """`first_touch` walks days and drops targets from its pool as they are hit, which the
    direct join it replaced could not do without materialising billions of rows. The
    behaviour that matters: the FIRST containing bar at or after `known_from`, never one
    before it, and nothing at all for a price the tape never reached."""
    import polars as pl

    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("research_q10", SCRIPTS / "q10_ladder.py")
    assert spec and spec.loader
    q10 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q10)

    minute = 60_000
    start = 1_772_323_200_000
    candles = pl.DataFrame(
        {
            "coin": ["BTC"] * 5,
            "ts_ms": [start + n * minute for n in range(5)],
            "h": [105.0, 101.0, 104.0, 101.0, 100.5],
            "l": [95.0, 99.0, 96.0, 99.0, 99.5],
        }
    )
    targets = pl.DataFrame(
        {
            "key": ["late", "early", "never"],
            "coin": ["BTC"] * 3,
            "px": [96.5, 96.5, 200.0],
            # `late` cannot use bar 0, which contained 96.5 before it was knowable.
            "known_from": [start + minute, start, start],
        }
    )
    found = dict(
        zip(*q10.first_touch(targets, candles).to_dict(as_series=False).values(), strict=True)
    )
    assert found["early"] == start
    assert found["late"] == start + 2 * minute
    assert "never" not in found
