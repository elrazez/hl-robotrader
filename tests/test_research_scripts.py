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

# script stem -> the report it must write under the out dir.
QUESTIONS = {
    "q1_reversion": "q1_reversion.md",
    "q8_path": "q8_path.md",
}


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


@pytest.mark.parametrize("name,report", sorted(QUESTIONS.items()))
def test_the_script_runs_on_the_fixture_lake_and_writes_a_report_with_a_decision(
    name: str, report: str, tmp_path: Path, research_lake: Path
) -> None:
    run(name, tmp_path, research_lake)
    text = (tmp_path / report).read_text()
    verdict = re.search(r"^## DECISION: (yes|no|inconclusive)$", text, re.MULTILINE)
    assert verdict, f"{report} has no parseable DECISION line"
    assert "net-stress" in text, f"{report} never mentions the cost basis every DECISION reads"
