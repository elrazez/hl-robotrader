"""Repo rules from CLAUDE.md that are cheaper to enforce than to remember.

One test per rule, each naming the offending file, because a convention nothing checks
is a convention that holds until the next module.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "liqfade"


def _sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_no_module_decodes_json_with_the_stdlib() -> None:
    """CLAUDE.md section 4: orjson for all JSON.

    `httpx.Response.json()` is stdlib `json` behind a method name that does not say so,
    which is how two modules ended up on it without anyone noticing.
    """
    offenders = [
        f"{path.relative_to(PACKAGE.parent)}:{number}"
        for path in _sources()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if "response.json()" in line or "resp.json()" in line
    ]
    assert offenders == [], f"use orjson.loads(response.content) instead: {offenders}"


def test_the_effective_time_rule_is_written_once() -> None:
    """CLAUDE.md rule 1: one place decides when a row became knowable.

    `rx_ms` over `ts_ms` is the whole no-lookahead rule, and it had been spelled out four
    times: `features/grid.py`, `backtest/data.py`, `backtest/engine.py` and
    `backtest/source.py`. Four copies is four chances for one of them to drift to `>= 0`,
    which would silently put the backfilled year on receive time it never had. The
    expression lives in `features/grid.py:effective_ts` and everything else calls it.
    """
    home = PACKAGE / "features" / "grid.py"
    offenders = [
        f"{path.relative_to(PACKAGE.parent)}:{number}"
        for path in _sources()
        if path != home
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if 'pl.col("rx_ms") > 0' in line
    ]
    assert offenders == [], f"call features.grid.effective_ts() instead: {offenders}"
